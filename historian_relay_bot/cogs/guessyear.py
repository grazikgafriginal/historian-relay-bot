from __future__ import annotations

import asyncio
import datetime
import json
import logging
import random
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import discord
from discord.ext import commands

log = logging.getLogger(__name__)

YEAR_RE = re.compile(r"^\s*(\d{1,4})\s*$")


@dataclass(slots=True)
class RoundState:
    round_id: int
    guild_id: int
    channel_id: int
    event_id: str
    correct_year: int
    prompt: str
    hints: List[str]
    started_at: int
    ends_at: int
    hints_used: int


class GuessYearCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

        self._active: Dict[Tuple[int, int], RoundState] = {}
        self._end_tasks: Dict[Tuple[int, int], asyncio.Task] = {}
        self._guess_cooldown: Dict[Tuple[int, int, int], int] = {}  # (guild, channel, user) -> last_ts

        self._events_by_id: Dict[str, Dict[str, Any]] = {}
        self._events: List[Dict[str, Any]] = []

        self._restore_started = False

        self._load_dataset()

    async def cog_load(self) -> None:
        # Restore active rounds once after the bot is ready.
        if not self._restore_started:
            self._restore_started = True
            asyncio.create_task(self._restore_after_ready())

    # ---------- dataset / restore ----------

    def _load_dataset(self) -> None:
        data_path = Path(__file__).resolve().parent.parent / "data" / "guessyear_events.json"
        if not data_path.exists():
            log.error("GuessYear dataset missing at %s", data_path)
            self._events = []
            self._events_by_id = {}
            return

        try:
            raw = json.loads(data_path.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                raise ValueError("dataset must be a list of events")
            self._events = [e for e in raw if isinstance(e, dict) and "id" in e and "year" in e and "prompt" in e]
            self._events_by_id = {str(e["id"]): e for e in self._events}
            log.info("Loaded GuessYear dataset: %d events", len(self._events))
        except Exception:
            log.exception("Failed to load GuessYear dataset")
            self._events = []
            self._events_by_id = {}

    async def _restore_after_ready(self) -> None:
        await self.bot.wait_until_ready()
        if not self.bot.cfg.GUESSYEAR_ENABLED:
            return

        now = int(time.time())
        try:
            rows = await self.bot.db.guessyear_list_active_rounds(now)
        except Exception:
            log.exception("Failed to restore GuessYear rounds")
            return

        restored = 0
        for r in rows:
            try:
                guild_id = int(r["guild_id"])
                channel_id = int(r["channel_id"])
                key = (guild_id, channel_id)

                evt = self._events_by_id.get(str(r["event_id"]))
                if not evt:
                    await self.bot.db.guessyear_mark_round_cancelled(int(r["round_id"]))
                    continue

                state = RoundState(
                    round_id=int(r["round_id"]),
                    guild_id=guild_id,
                    channel_id=channel_id,
                    event_id=str(r["event_id"]),
                    correct_year=int(r["correct_year"]),
                    prompt=str(evt["prompt"]),
                    hints=list(evt.get("hints", [])),
                    started_at=int(r["started_at"]),
                    ends_at=int(r["ends_at"]),
                    hints_used=int(r.get("hints_used", 0)),
                )

                self._active[key] = state
                self._schedule_end(state)
                restored += 1
            except Exception:
                log.exception("Failed restoring one GuessYear round")

        if restored:
            log.info("Restored %d GuessYear active rounds", restored)

    # ---------- helpers ----------

    def _is_allowed_channel(self, channel_id: int) -> bool:
        allowed = getattr(self.bot.cfg, "GUESSYEAR_ALLOWED_CHANNEL_IDS", [])
        return (not allowed) or (channel_id in allowed)

    def _resolve_max_year(self) -> int:
        mx = int(getattr(self.bot.cfg, "GUESSYEAR_MAX_YEAR", 0) or 0)
        return mx if mx > 0 else datetime.datetime.utcnow().year

    def _pick_event(self) -> Optional[Dict[str, Any]]:
        if not self._events:
            return None
        return random.choice(self._events)

    def _remaining(self, ends_at: int) -> int:
        return max(0, ends_at - int(time.time()))

    def _schedule_end(self, state: RoundState) -> None:
        key = (state.guild_id, state.channel_id)

        old = self._end_tasks.get(key)
        if old and not old.done():
            old.cancel()

        self._end_tasks[key] = asyncio.create_task(self._end_round_when_ready(state))

    async def _end_round_when_ready(self, state: RoundState) -> None:
        delay = max(0, state.ends_at - int(time.time()))
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return

        key = (state.guild_id, state.channel_id)
        current = self._active.get(key)
        if not current or current.round_id != state.round_id:
            return

        await self._end_round(state.guild_id, state.channel_id, forced=False)

    async def _ensure_state_loaded(self, guild_id: int, channel_id: int) -> Optional[RoundState]:
        key = (guild_id, channel_id)
        if key in self._active:
            return self._active[key]

        try:
            row = await self.bot.db.guessyear_get_active_round(guild_id, channel_id, int(time.time()))
        except Exception:
            return None

        if not row:
            return None

        evt = self._events_by_id.get(str(row["event_id"]))
        if not evt:
            await self.bot.db.guessyear_mark_round_cancelled(int(row["round_id"]))
            return None

        state = RoundState(
            round_id=int(row["round_id"]),
            guild_id=int(row["guild_id"]),
            channel_id=int(row["channel_id"]),
            event_id=str(row["event_id"]),
            correct_year=int(row["correct_year"]),
            prompt=str(evt["prompt"]),
            hints=list(evt.get("hints", [])),
            started_at=int(row["started_at"]),
            ends_at=int(row["ends_at"]),
            hints_used=int(row.get("hints_used", 0)),
        )

        self._active[key] = state
        self._schedule_end(state)
        return state

    # ---------- round end / announce ----------

    async def _end_round(self, guild_id: int, channel_id: int, forced: bool) -> None:
        key = (guild_id, channel_id)
        state = self._active.get(key)
        if not state:
            return

        try:
            did_end = await self.bot.db.guessyear_try_end_round(state.round_id)
        except Exception:
            log.exception("Failed to end GuessYear round in DB")
            return

        if not did_end:
            # Another task/process already ended and announced.
            self._active.pop(key, None)
            return

        # Pull guesses
        try:
            guesses = await self.bot.db.guessyear_list_guesses(state.round_id)
        except Exception:
            log.exception("Failed to fetch GuessYear guesses")
            guesses = []

        # Score guesses (diff asc, earliest ts wins ties)
        scored: List[Tuple[int, int, int, int]] = []  # (abs_diff, guessed_at, user_id, guess_year)
        for g in guesses:
            try:
                uid = int(g["user_id"])
                gy = int(g["guess_year"])
                ts = int(g["guessed_at"])
                diff = abs(gy - state.correct_year)
                scored.append((diff, ts, uid, gy))
            except Exception:
                continue

        scored.sort(key=lambda x: (x[0], x[1]))

        winner_user_id: Optional[int] = None
        winner_guess: Optional[int] = None
        winner_diff: Optional[int] = None
        if scored:
            winner_diff, _ts, winner_user_id, winner_guess = scored[0]

        # Stats: count plays for all guessers; count win for winner.
        try:
            await self.bot.db.guessyear_stats_record_play(guild_id, [int(g["user_id"]) for g in guesses])
            if winner_user_id is not None:
                await self.bot.db.guessyear_stats_record_win(guild_id, int(winner_user_id))
        except Exception:
            # Stats are optional; never fail the round end.
            pass

        # Fetch channel
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except Exception:
                channel = None

        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            unique_players = {int(g["user_id"]) for g in guesses}
            total_guesses = len(guesses)
            total_players = len(unique_players)

            lines: List[str] = []
            lines.append(f"**Prompt:** {state.prompt}")
            lines.append(f"**Correct year:** **{state.correct_year}**")
            lines.append(f"**Guesses:** **{total_guesses}** ({total_players} player{'s' if total_players != 1 else ''})")

            if forced:
                lines.append("_Round was ended by a moderator._")

            if winner_user_id is None:
                lines.append("\nNo valid guesses were submitted. No winner this round.")
            else:
                perfect = " 🎯" if (winner_diff == 0) else ""
                lines.append(
                    f"\n🏆 **Winner:** <@{winner_user_id}> (guessed **{winner_guess}**, off by **{winner_diff}**){perfect}"
                )

                # Top 3 closest
                top = scored[:3]
                lines.append("\n**Top 3 closest:**")
                for i, (diff, ts, uid, gy) in enumerate(top, start=1):
                    delta = gy - state.correct_year
                    sign = "+" if delta > 0 else ""  # negative already has '-'
                    lines.append(f"{i}. <@{uid}> — **{gy}** (off by **{diff}**)")

            msg = f"**🕰️ Guess the Year — Round #{state.round_id} ended**\n\n" + "\n".join(lines)
            await channel.send(msg)

        # Cleanup memory + timer
        self._active.pop(key, None)
        task = self._end_tasks.pop(key, None)
        if task and not task.done():
            task.cancel()

    # ---------- commands ----------

    @commands.group(name="guessyear", invoke_without_command=True)
    async def guessyear(self, ctx: commands.Context):
        """Start a Guess the Year round in this channel."""
        if not ctx.guild or not isinstance(ctx.channel, (discord.TextChannel, discord.Thread)):
            return

        if not self.bot.cfg.GUESSYEAR_ENABLED:
            return await ctx.send("Guess the Year is disabled on this server.", delete_after=10)

        if not self._is_allowed_channel(ctx.channel.id):
            return await ctx.send("Guess the Year is not enabled in this channel.", delete_after=10)

        state = await self._ensure_state_loaded(ctx.guild.id, ctx.channel.id)
        if state and state.ends_at > int(time.time()):
            rem = self._remaining(state.ends_at)
            return await ctx.send(
                f"A round is already active here. **{rem}s** remaining. "
                f"Type a year (e.g. `1066`) to guess. Use `!hint`.",
                delete_after=12,
            )

        evt = self._pick_event()
        if not evt:
            return await ctx.send("Sorry — the GuessYear dataset is missing or empty.", delete_after=12)

        min_year = int(self.bot.cfg.GUESSYEAR_MIN_YEAR)
        max_year = self._resolve_max_year()
        correct_year = int(evt["year"])
        if correct_year < min_year or correct_year > max_year:
            return await ctx.send("Dataset event has an out-of-range year. Please fix the dataset.", delete_after=12)

        now = int(time.time())
        ends_at = now + int(self.bot.cfg.GUESSYEAR_ROUND_SECONDS)

        try:
            round_id = await self.bot.db.guessyear_create_round(
                guild_id=ctx.guild.id,
                channel_id=ctx.channel.id,
                started_by_user_id=ctx.author.id,
                event_id=str(evt["id"]),
                correct_year=correct_year,
                started_at=now,
                ends_at=ends_at,
            )
        except sqlite3.IntegrityError:
            # Another task already created an active round.
            state = await self._ensure_state_loaded(ctx.guild.id, ctx.channel.id)
            if state and state.ends_at > int(time.time()):
                rem = self._remaining(state.ends_at)
                return await ctx.send(
                    f"A round is already active here. **{rem}s** remaining. "
                    f"Type a year (e.g. `1066`) to guess. Use `!hint`.",
                    delete_after=12,
                )
            return await ctx.send("A round is already active here.", delete_after=10)
        except Exception:
            log.exception("Failed to create GuessYear round in DB")
            return await ctx.send("Could not start a round (database error).", delete_after=12)

        state = RoundState(
            round_id=int(round_id),
            guild_id=ctx.guild.id,
            channel_id=ctx.channel.id,
            event_id=str(evt["id"]),
            correct_year=correct_year,
            prompt=str(evt["prompt"]),
            hints=list(evt.get("hints", [])),
            started_at=now,
            ends_at=ends_at,
            hints_used=0,
        )
        self._active[(ctx.guild.id, ctx.channel.id)] = state
        self._schedule_end(state)

        await ctx.send(
            f"**🕰️ Guess the Year #{round_id}**\n"
            f"**Prompt:** {state.prompt}\n\n"
            f"Submit your guess as a year (e.g., `1066`).\n"
            f"Time: **{self.bot.cfg.GUESSYEAR_ROUND_SECONDS}s**. Use `!hint` for clues."
        )

    @guessyear.command(name="status")
    async def guessyear_status(self, ctx: commands.Context):
        if not ctx.guild:
            return
        state = await self._ensure_state_loaded(ctx.guild.id, ctx.channel.id)
        if not state or state.ends_at <= int(time.time()):
            return await ctx.send("No active Guess the Year round in this channel.", delete_after=10)

        rem = self._remaining(state.ends_at)
        await ctx.send(
            f"🕰️ Round #{state.round_id} is active. **{rem}s** remaining.\n"
            f"Hints used: **{state.hints_used}/{self.bot.cfg.GUESSYEAR_MAX_HINTS}**.\n"
            f"Guess by typing a year like `1789`."
        )

    @guessyear.command(name="stop")
    async def guessyear_stop(self, ctx: commands.Context):
        if not ctx.guild:
            return

        # mod role or Manage Messages
        is_mod_role = False
        has_perm = False
        if isinstance(ctx.author, discord.Member):
            is_mod_role = any(r.id == self.bot.cfg.MOD_ROLE_ID for r in ctx.author.roles)
            has_perm = ctx.author.guild_permissions.manage_messages

        if not (is_mod_role or has_perm):
            return await ctx.send("You don't have permission to stop the round.", delete_after=10)

        state = await self._ensure_state_loaded(ctx.guild.id, ctx.channel.id)
        if not state:
            return await ctx.send("No active round to stop in this channel.", delete_after=10)

        await ctx.send("Ending the current round…", delete_after=5)
        await self._end_round(ctx.guild.id, ctx.channel.id, forced=True)

    ### Guess Year Leaderboard
    @guessyear.command(name="top")
    async def guessyear_top(self, ctx: commands.Context, limit: int = 10):
        """Show the GuessYear leaderboard for this server."""
        if not ctx.guild:
            return

        limit = max(1, min(int(limit), 25))

        try:
            rows = await self.bot.db.guessyear_stats_get_top(ctx.guild.id, limit)
        except Exception:
            log.exception("Failed reading GuessYear leaderboard")
            return await ctx.send("Could not fetch leaderboard (database error).", delete_after=10)

        if not rows:
            return await ctx.send("No GuessYear stats yet. Play a few rounds first!", delete_after=10)

        def rank_prefix(rank: int) -> str:
            if rank == 1:
                return "🥇"
            if rank == 2:
                return "🥈"
            if rank == 3:
                return "🥉"
            return f"#{rank}"

        lines = []
        for i, r in enumerate(rows, start=1):
            uid = int(r["user_id"])
            wins = int(r["wins"])
            plays = int(r["plays"])
            member = ctx.guild.get_member(uid)
            mention = member.mention if member else f"<@{uid}>"

            if not member:
                try:
                    member = await ctx.guild.fetch_member(uid)
                except Exception:
                    member = None

            raw_name = member.display_name if member else f"User {uid}"
            name = discord.utils.escape_markdown(raw_name)
            rate = (wins / plays * 100.0) if plays > 0 else 0.0

            lines.append(
                f"{rank_prefix(i)} **{mention}**\n"
                f"🏆 **{wins}** wins • 🎲 **{plays}** plays • 📈 **{rate:.0f}%** win rate"
            )

        embed = discord.Embed(
            title="🏅 Guess Year Leaderboard",
            description="\n\n".join(lines),
            color=discord.Color.gold(),
        )

        embed.set_footer(
            text=f"Server: {ctx.guild.name} • Top {min(len(rows), limit)} players"
        )

        await ctx.send(embed=embed)

    @guessyear.command(name="me")
    async def guessyear_me(self, ctx: commands.Context):
        """Show your GuessYear stats in this server."""
        if not ctx.guild:
            return

        try:
            row = await self.bot.db.guessyear_stats_get_user(ctx.guild.id, ctx.author.id)
        except Exception:
            log.exception("Failed reading GuessYear user stats")
            return await ctx.send("Could not fetch your stats (database error).", delete_after=10)

        if not row:
            return await ctx.send("You don't have any GuessYear stats yet — play a round to get started!", delete_after=12)

        wins = int(row["wins"])
        plays = int(row["plays"])
        rate = (wins / plays * 100.0) if plays > 0 else 0.0
        rank = int(row["rank"]) if row.get("rank") is not None else None
        total = int(row["total"]) if row.get("total") is not None else None
        last_played = int(row.get("last_played_at") or 0)

        when = f"<t:{last_played}:R>" if last_played else "never"
        rank_str = f"#{rank} of {total}" if rank and total else "(unranked)"

        await ctx.send(
            "\n".join(
                [
                    f"**📊 GuessYear stats for {ctx.author.mention}**",
                    f"Rank: **{rank_str}**",
                    f"Wins: **{wins}**",
                    f"Plays: **{plays}**",
                    f"Win rate: **{rate:.0f}%**",
                    f"Last played: **{when}**",
                ]
            )
        )

    @commands.command(name="hint")
    async def hint(self, ctx: commands.Context):
        if not ctx.guild:
            return
        if not self.bot.cfg.GUESSYEAR_HINTS_ENABLED:
            return await ctx.send("Hints are disabled.", delete_after=8)

        state = await self._ensure_state_loaded(ctx.guild.id, ctx.channel.id)
        if not state or state.ends_at <= int(time.time()):
            return await ctx.send("No active round in this channel.", delete_after=10)

        max_hints = int(self.bot.cfg.GUESSYEAR_MAX_HINTS)
        if state.hints_used >= max_hints:
            return await ctx.send("No more hints available this round.", delete_after=10)

        if not state.hints:
            return await ctx.send("This event has no hints configured.", delete_after=10)

        next_index = state.hints_used
        if next_index >= len(state.hints):
            return await ctx.send("No more hints available for this event.", delete_after=10)

        try:
            new_used = await self.bot.db.guessyear_increment_hints_used(state.round_id)
        except Exception:
            log.exception("Failed incrementing hints_used")
            return await ctx.send("Could not fetch a hint (database error).", delete_after=10)

        state.hints_used = int(new_used)
        hint_text = state.hints[next_index]
        await ctx.send(f"💡 Hint {next_index+1}/{max_hints}: **{hint_text}**")

    @commands.command(name="guess")
    async def guess_cmd(self, ctx: commands.Context, year: str):
        # Explicit command form: !guess 1789
        await self._handle_guess(ctx.message, override_text=year)

    # ---------- message listener (guesses) ----------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Treat plain year messages as guesses.
        if message.author.bot:
            return
        if not message.guild:
            return
        if message.content and message.content.lstrip().startswith("!"):
            return

        await self._handle_guess(message, override_text=None)

    async def _handle_guess(self, message: discord.Message, override_text: Optional[str]):
        if not message.guild:
            return
        if not isinstance(message.channel, (discord.TextChannel, discord.Thread)):
            return
        if not self.bot.cfg.GUESSYEAR_ENABLED:
            return
        if not self._is_allowed_channel(message.channel.id):
            return

        state = await self._ensure_state_loaded(message.guild.id, message.channel.id)
        if not state or state.ends_at <= int(time.time()):
            return

        now = int(time.time())

        # cooldown anti-spam
        key_cd = (message.guild.id, message.channel.id, message.author.id)
        last = self._guess_cooldown.get(key_cd, 0)
        if now - last < int(self.bot.cfg.GUESSYEAR_COOLDOWN_SECONDS):
            return
        self._guess_cooldown[key_cd] = now

        text = override_text if override_text is not None else message.content
        m = YEAR_RE.match(text or "")
        if not m:
            return

        guess_year = int(m.group(1))
        min_year = int(self.bot.cfg.GUESSYEAR_MIN_YEAR)
        max_year = self._resolve_max_year()
        if guess_year < min_year or guess_year > max_year:
            return

        policy = str(getattr(self.bot.cfg, "GUESSYEAR_GUESS_POLICY", "first")).lower().strip()
        if policy not in ("first", "latest"):
            policy = "first"

        try:
            ok, already = await self.bot.db.guessyear_upsert_guess(
                round_id=state.round_id,
                user_id=message.author.id,
                guess_year=guess_year,
                guessed_at=now,
                policy=policy,
            )
        except Exception:
            log.exception("Failed saving guess")
            return

        if not ok:
            return

        if already and policy == "first":
            return

        try:
            await message.channel.send(f"✅ {message.author.mention} guessed **{guess_year}**.")
        except Exception:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(GuessYearCog(bot))
