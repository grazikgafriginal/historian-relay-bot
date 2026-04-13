from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import discord
from discord.ext import commands

log = logging.getLogger(__name__)
YEAR_RE = re.compile(r"^\s*(-?\d{1,4})\s*$")


@dataclass(slots=True)
class TournamentSignupState:
    tournament_id: int
    guild_id: int
    channel_id: int
    created_by_user_id: int
    created_at: int
    signup_message_id: int | None
    title: str
    bracket_size: int
    best_of: int
    entrants: set[int] = field(default_factory=set)
    status: str = "signup"


@dataclass(slots=True)
class TournamentMatchResult:
    question_number: int
    event_id: str
    prompt: str
    correct_year: int
    guesses: dict[int, int] = field(default_factory=dict)
    winner_user_id: int | None = None
    winner_guess: int | None = None
    winner_diff: int | None = None


@dataclass(slots=True)
class TournamentMatchState:
    match_id: int
    tournament_id: int
    round_number: int
    bracket_position: int
    guild_id: int
    host_channel_id: int
    thread_id: int
    player1_user_id: int
    player2_user_id: int
    best_of: int
    created_by_user_id: int
    event_id: str
    correct_year: int
    prompt: str
    current_question: int = 1
    round_started_at: int = 0
    round_ends_at: int = 0
    scores: dict[int, int] = field(default_factory=dict)
    guesses: dict[int, tuple[int, int]] = field(default_factory=dict)
    history: list[TournamentMatchResult] = field(default_factory=list)
    finished: bool = False
    winner_user_id: int | None = None
    status_message_id: int | None = None

    @property
    def wins_needed(self) -> int:
        return self.best_of // 2 + 1


@dataclass(slots=True)
class TournamentState:
    tournament_id: int
    guild_id: int
    channel_id: int
    created_by_user_id: int
    title: str
    bracket_size: int
    best_of: int
    signup_message_id: int | None
    entrants: list[int] = field(default_factory=list)
    round_number: int = 0
    active_match_ids: set[int] = field(default_factory=set)
    next_round_players: list[int] = field(default_factory=list)
    status: str = "signup"
    winner_user_id: int | None = None


class TournamentGuessModal(discord.ui.Modal, title="Submit hidden tournament guess"):
    guess = discord.ui.TextInput(
        label="Year",
        placeholder="Enter a year like 1789",
        min_length=1,
        max_length=5,
        required=True,
    )

    def __init__(self, cog: "DuelTournamentCog", match: TournamentMatchState):
        super().__init__(timeout=120)
        self.cog = cog
        self.match = match

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog._submit_hidden_guess(interaction, self.match, str(self.guess))


class TournamentSignupView(discord.ui.View):
    def __init__(self, cog: "DuelTournamentCog", state: TournamentSignupState):
        super().__init__(timeout=None)
        self.cog = cog
        self.state = state
        self.message: discord.Message | None = None

    def _is_mod(self, interaction: discord.Interaction) -> bool:
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        return bool(member and self.cog._can_manage(member))

    @discord.ui.button(label="Join", style=discord.ButtonStyle.success, emoji="⚔️")
    async def join_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog._join_signup(interaction, self.state)

    @discord.ui.button(label="Leave", style=discord.ButtonStyle.secondary)
    async def leave_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog._leave_signup(interaction, self.state)

    @discord.ui.button(label="Start", style=discord.ButtonStyle.primary, emoji="🏁")
    async def start_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self._is_mod(interaction):
            await interaction.response.send_message("Only moderators can start the tournament.", ephemeral=True)
            return
        await self.cog._start_signup_tournament(interaction, self.state)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self._is_mod(interaction):
            await interaction.response.send_message("Only moderators can cancel the tournament.", ephemeral=True)
            return
        await self.cog._cancel_signup(interaction, self.state)


class TournamentMatchView(discord.ui.View):
    def __init__(self, cog: "DuelTournamentCog", match: TournamentMatchState):
        super().__init__(timeout=None)
        self.cog = cog
        self.match = match

    @discord.ui.button(label="Submit hidden guess", style=discord.ButtonStyle.primary, emoji="🕵️")
    async def submit_hidden_guess(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog._open_guess_modal(interaction, self.match)


class DuelTournamentCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._events: list[dict[str, Any]] = []
        self._events_by_id: dict[str, dict[str, Any]] = {}
        self._signups: dict[tuple[int, int], TournamentSignupState] = {}
        self._tournaments: dict[int, TournamentState] = {}
        self._matches: dict[int, TournamentMatchState] = {}
        self._match_tasks: dict[int, asyncio.Task[Any]] = {}
        self._load_dataset()

    async def cog_load(self) -> None:
        await self._ensure_schema()

    async def cog_unload(self) -> None:
        for task in self._match_tasks.values():
            if not task.done():
                task.cancel()

    def _load_dataset(self) -> None:
        data_path = Path(__file__).resolve().parent.parent / "data" / "guessyear_events.json"
        try:
            raw = json.loads(data_path.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                raise ValueError("guessyear_events.json must contain a list")
            self._events = [evt for evt in raw if isinstance(evt, dict) and evt.get("id") and evt.get("prompt") and evt.get("year")]
            self._events_by_id = {str(evt["id"]): evt for evt in self._events}
            log.info("Loaded DuelTournament dataset: %d events", len(self._events))
        except Exception:
            log.exception("Failed to load GuessYear dataset for DuelTournament")
            self._events = []
            self._events_by_id = {}

    async def _ensure_schema(self) -> None:
        if not getattr(self.bot, "db", None) or not getattr(self.bot.db, "conn", None):
            return
        await self.bot.db.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS guessyear_tournaments (
              tournament_id INTEGER PRIMARY KEY AUTOINCREMENT,
              guild_id TEXT NOT NULL,
              channel_id TEXT NOT NULL,
              created_by_user_id TEXT NOT NULL,
              title TEXT NOT NULL,
              bracket_size INTEGER NOT NULL DEFAULT 8,
              best_of INTEGER NOT NULL DEFAULT 3,
              status TEXT NOT NULL DEFAULT 'signup',
              signup_message_id TEXT,
              created_at INTEGER NOT NULL,
              started_at INTEGER,
              ended_at INTEGER,
              winner_user_id TEXT
            );

            CREATE TABLE IF NOT EXISTS guessyear_tournament_entries (
              tournament_id INTEGER NOT NULL,
              user_id TEXT NOT NULL,
              joined_at INTEGER NOT NULL,
              seed INTEGER,
              PRIMARY KEY (tournament_id, user_id),
              FOREIGN KEY (tournament_id) REFERENCES guessyear_tournaments(tournament_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS guessyear_tournament_matches (
              match_id INTEGER PRIMARY KEY AUTOINCREMENT,
              tournament_id INTEGER NOT NULL,
              round_number INTEGER NOT NULL,
              bracket_position INTEGER NOT NULL,
              player1_user_id TEXT,
              player2_user_id TEXT,
              winner_user_id TEXT,
              thread_id TEXT,
              status TEXT NOT NULL DEFAULT 'pending',
              best_of INTEGER NOT NULL DEFAULT 3,
              score1 INTEGER NOT NULL DEFAULT 0,
              score2 INTEGER NOT NULL DEFAULT 0,
              started_at INTEGER,
              ended_at INTEGER,
              FOREIGN KEY (tournament_id) REFERENCES guessyear_tournaments(tournament_id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_guessyear_tournaments_status
            ON guessyear_tournaments(guild_id, channel_id, status, created_at);

            CREATE INDEX IF NOT EXISTS idx_guessyear_tournament_matches_round
            ON guessyear_tournament_matches(tournament_id, round_number, status);
            """
        )
        await self.bot.db.conn.commit()

    def _can_manage(self, member: discord.Member) -> bool:
        cfg = getattr(self.bot, "cfg", None)
        mod_role_id = int(getattr(cfg, "MOD_ROLE_ID", 0) or 0)
        has_mod_role = mod_role_id and any(role.id == mod_role_id for role in member.roles)
        return bool(has_mod_role or member.guild_permissions.manage_messages)

    def _is_allowed_channel(self, channel_id: int) -> bool:
        allowed = list(getattr(self.bot.cfg, "GUESSYEAR_ALLOWED_CHANNEL_IDS", []) or [])
        return not allowed or channel_id in allowed

    def _pick_event(self) -> dict[str, Any] | None:
        if not self._events:
            return None
        min_year = int(getattr(self.bot.cfg, "GUESSYEAR_MIN_YEAR", 1) or 1)
        max_year = int(getattr(self.bot.cfg, "GUESSYEAR_MAX_YEAR", 0) or 0)
        if max_year <= 0:
            max_year = time.gmtime().tm_year
        pool = [
            evt for evt in self._events
            if min_year <= int(evt.get("year", 0)) <= max_year
        ]
        return random.choice(pool or self._events)

    async def _create_tournament_row(
        self,
        guild_id: int,
        channel_id: int,
        created_by_user_id: int,
        title: str,
        bracket_size: int,
        best_of: int,
        created_at: int,
    ) -> int:
        cur = await self.bot.db.conn.execute(
            """
            INSERT INTO guessyear_tournaments (
              guild_id, channel_id, created_by_user_id, title,
              bracket_size, best_of, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'signup', ?)
            """,
            (
                str(guild_id),
                str(channel_id),
                str(created_by_user_id),
                title,
                int(bracket_size),
                int(best_of),
                int(created_at),
            ),
        )
        await self.bot.db.conn.commit()
        return int(cur.lastrowid)

    async def _set_signup_message_id(self, tournament_id: int, message_id: int) -> None:
        await self.bot.db.conn.execute(
            "UPDATE guessyear_tournaments SET signup_message_id=? WHERE tournament_id=?",
            (str(message_id), int(tournament_id)),
        )
        await self.bot.db.conn.commit()

    async def _set_tournament_status(self, tournament_id: int, status: str, *, winner_user_id: int | None = None) -> None:
        now = int(time.time())
        if status == "active":
            await self.bot.db.conn.execute(
                "UPDATE guessyear_tournaments SET status=?, started_at=? WHERE tournament_id=?",
                (status, now, int(tournament_id)),
            )
        elif status in {"finished", "cancelled"}:
            await self.bot.db.conn.execute(
                "UPDATE guessyear_tournaments SET status=?, ended_at=?, winner_user_id=? WHERE tournament_id=?",
                (status, now, str(winner_user_id) if winner_user_id else None, int(tournament_id)),
            )
        else:
            await self.bot.db.conn.execute(
                "UPDATE guessyear_tournaments SET status=? WHERE tournament_id=?",
                (status, int(tournament_id)),
            )
        await self.bot.db.conn.commit()

    async def _upsert_entry(self, tournament_id: int, user_id: int) -> None:
        await self.bot.db.conn.execute(
            """
            INSERT INTO guessyear_tournament_entries (tournament_id, user_id, joined_at)
            VALUES (?, ?, ?)
            ON CONFLICT(tournament_id, user_id) DO UPDATE SET joined_at=excluded.joined_at
            """,
            (int(tournament_id), str(user_id), int(time.time())),
        )
        await self.bot.db.conn.commit()

    async def _remove_entry(self, tournament_id: int, user_id: int) -> None:
        await self.bot.db.conn.execute(
            "DELETE FROM guessyear_tournament_entries WHERE tournament_id=? AND user_id=?",
            (int(tournament_id), str(user_id)),
        )
        await self.bot.db.conn.commit()

    async def _create_match_row(
        self,
        tournament_id: int,
        round_number: int,
        bracket_position: int,
        player1_user_id: int,
        player2_user_id: int,
        best_of: int,
    ) -> int:
        cur = await self.bot.db.conn.execute(
            """
            INSERT INTO guessyear_tournament_matches (
              tournament_id, round_number, bracket_position,
              player1_user_id, player2_user_id, status, best_of
            ) VALUES (?, ?, ?, ?, ?, 'active', ?)
            """,
            (
                int(tournament_id),
                int(round_number),
                int(bracket_position),
                str(player1_user_id),
                str(player2_user_id),
                int(best_of),
            ),
        )
        await self.bot.db.conn.commit()
        return int(cur.lastrowid)

    async def _update_match_thread(self, match_id: int, thread_id: int) -> None:
        await self.bot.db.conn.execute(
            "UPDATE guessyear_tournament_matches SET thread_id=?, started_at=? WHERE match_id=?",
            (str(thread_id), int(time.time()), int(match_id)),
        )
        await self.bot.db.conn.commit()

    async def _update_match_result(self, match: TournamentMatchState) -> None:
        score1 = int(match.scores.get(match.player1_user_id, 0))
        score2 = int(match.scores.get(match.player2_user_id, 0))
        await self.bot.db.conn.execute(
            """
            UPDATE guessyear_tournament_matches
            SET winner_user_id=?, status=?, score1=?, score2=?, ended_at=?
            WHERE match_id=?
            """,
            (
                str(match.winner_user_id) if match.winner_user_id else None,
                "finished" if match.finished else "active",
                score1,
                score2,
                int(time.time()) if match.finished else None,
                int(match.match_id),
            ),
        )
        await self.bot.db.conn.commit()

    def _build_signup_embed(self, state: TournamentSignupState, guild: discord.Guild) -> discord.Embed:
        entrant_lines: list[str] = []
        for uid in sorted(state.entrants):
            member = guild.get_member(uid)
            if member is not None:
                entrant_lines.append(f"{member.display_name} ({member.mention})")
            else:
                entrant_lines.append(f"<@{uid}>")

        description = (
            "Friday mini cup signups are open. Join now and the bot will seed a single-elimination bracket.\n\n"
            f"**Signup cap:** {state.bracket_size}\n"
            f"**Match format:** best of {state.best_of}\n"
            "**Start rule:** the tournament can start with any even number of entrants from 2 up to the signup cap.\n"
            "**Tiebreaker:** if both guesses are equally close, the earlier guess wins the point."
        )
        embed = discord.Embed(
            title=state.title,
            description=description,
            color=discord.Color.gold(),
        )
        embed.add_field(
            name=f"Entrants ({len(state.entrants)}/{state.bracket_size})",
            value="\n".join(entrant_lines) if entrant_lines else "No entrants yet.",
            inline=False,
        )
        embed.set_footer(text="Use the buttons below or !dueltourney join / !dueltourney leave.")
        return embed

    def _build_match_embed(self, match: TournamentMatchState, guild: discord.Guild) -> discord.Embed:
        p1 = guild.get_member(match.player1_user_id)
        p2 = guild.get_member(match.player2_user_id)
        s1 = match.scores.get(match.player1_user_id, 0)
        s2 = match.scores.get(match.player2_user_id, 0)
        embed = discord.Embed(
            title=f"Tournament Match • Round {match.round_number}",
            description=match.prompt,
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Players", value=f"{p1.mention if p1 else f'<@{match.player1_user_id}>'} vs {p2.mention if p2 else f'<@{match.player2_user_id}>'}", inline=False)
        embed.add_field(name="Score", value=f"{s1} - {s2}", inline=True)
        embed.add_field(name="Question", value=f"{match.current_question}/{match.best_of}", inline=True)
        embed.add_field(name="How to play", value="Use **Submit hidden guess** and enter a year.", inline=False)
        embed.set_footer(text="Earliest guess wins equal-distance ties.")
        return embed

    async def _refresh_signup_message(self, state: TournamentSignupState) -> None:
        channel = self.bot.get_channel(state.channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        if state.signup_message_id is None:
            return
        try:
            msg = await channel.fetch_message(state.signup_message_id)
        except Exception:
            return
        view = self._signup_view_for(state)
        view.message = msg
        await msg.edit(embed=self._build_signup_embed(state, channel.guild), view=view)

    def _signup_view_for(self, state: TournamentSignupState) -> TournamentSignupView:
        return TournamentSignupView(self, state)

    async def _join_signup(self, interaction: discord.Interaction, state: TournamentSignupState) -> None:
        if state.status != "signup":
            await interaction.response.send_message("This tournament is no longer accepting entrants.", ephemeral=True)
            return
        if len(state.entrants) >= state.bracket_size and interaction.user.id not in state.entrants:
            await interaction.response.send_message("This bracket is already full.", ephemeral=True)
            return
        state.entrants.add(interaction.user.id)
        await self._upsert_entry(state.tournament_id, interaction.user.id)
        await interaction.response.send_message("You joined the tournament.", ephemeral=True)
        await self._refresh_signup_message(state)

    async def _leave_signup(self, interaction: discord.Interaction, state: TournamentSignupState) -> None:
        if interaction.user.id not in state.entrants:
            await interaction.response.send_message("You are not currently in this tournament.", ephemeral=True)
            return
        state.entrants.discard(interaction.user.id)
        await self._remove_entry(state.tournament_id, interaction.user.id)
        await interaction.response.send_message("You left the tournament.", ephemeral=True)
        await self._refresh_signup_message(state)

    async def _cancel_signup(self, interaction: discord.Interaction, state: TournamentSignupState) -> None:
        self._signups.pop((state.guild_id, state.channel_id), None)
        state.status = "cancelled"
        await self._set_tournament_status(state.tournament_id, "cancelled")
        view = self._signup_view_for(state)
        for child in view.children:
            child.disabled = True
        embed = self._build_signup_embed(state, interaction.guild)
        embed.color = discord.Color.dark_grey()
        embed.title = f"{state.title} • Cancelled"
        await interaction.response.edit_message(embed=embed, view=view)

    async def _start_signup_tournament(self, interaction: discord.Interaction, state: TournamentSignupState) -> None:
        if state.status != "signup":
            await interaction.response.send_message("This tournament has already started.", ephemeral=True)
            return
        entrants = list(state.entrants)
        if len(entrants) < 2:
            await interaction.response.send_message("You need at least 2 entrants to start the tournament.", ephemeral=True)
            return
        if len(entrants) % 2 != 0:
            await interaction.response.send_message(
                "You currently have an odd number of entrants. Add one more player or remove one before starting.",
                ephemeral=True,
            )
            return
        random.shuffle(entrants)
        state.status = "active"
        self._signups.pop((state.guild_id, state.channel_id), None)
        actual_size = len(entrants)
        state.bracket_size = actual_size
        tournament = TournamentState(
            tournament_id=state.tournament_id,
            guild_id=state.guild_id,
            channel_id=state.channel_id,
            created_by_user_id=state.created_by_user_id,
            title=state.title,
            bracket_size=actual_size,
            best_of=state.best_of,
            signup_message_id=state.signup_message_id,
            entrants=entrants,
            round_number=1,
            status="active",
        )
        self._tournaments[tournament.tournament_id] = tournament
        await self._set_tournament_status(tournament.tournament_id, "active")

        view = self._signup_view_for(state)
        for child in view.children:
            child.disabled = True
        embed = self._build_signup_embed(state, interaction.guild)
        embed.title = f"{state.title} • Started"
        await interaction.response.edit_message(embed=embed, view=view)

        await self._start_round(interaction.channel, tournament, entrants)

    async def _start_round(self, host_channel: discord.abc.Messageable, tournament: TournamentState, players: list[int]) -> None:
        round_number = tournament.round_number
        tournament.next_round_players = []
        tournament.active_match_ids.clear()

        pairings: list[tuple[int, int | None]] = []
        work = list(players)
        while work:
            player1 = work.pop(0)
            player2 = work.pop(0) if work else None
            pairings.append((player1, player2))

        bracket_lines: list[str] = []
        guild = self.bot.get_guild(tournament.guild_id)
        for idx, (player1, player2) in enumerate(pairings, start=1):
            if player2 is None:
                tournament.next_round_players.append(player1)
                label1 = guild.get_member(player1).mention if guild and guild.get_member(player1) else f"<@{player1}>"
                bracket_lines.append(f"Match {idx}: {label1} gets a bye")
                continue

            label1 = guild.get_member(player1).mention if guild and guild.get_member(player1) else f"<@{player1}>"
            label2 = guild.get_member(player2).mention if guild and guild.get_member(player2) else f"<@{player2}>"
            bracket_lines.append(f"Match {idx}: {label1} vs {label2}")
            match = await self._launch_match(host_channel, tournament, round_number, idx, player1, player2)
            tournament.active_match_ids.add(match.match_id)

        embed = discord.Embed(
            title=f"{tournament.title} • Round {round_number}",
            description="\n".join(bracket_lines) if bracket_lines else "No matches created.",
            color=discord.Color.gold(),
        )
        await host_channel.send(embed=embed)

        if not tournament.active_match_ids:
            await self._advance_or_finish(host_channel, tournament)

    async def _launch_match(
        self,
        host_channel: discord.abc.Messageable,
        tournament: TournamentState,
        round_number: int,
        bracket_position: int,
        player1_user_id: int,
        player2_user_id: int,
    ) -> TournamentMatchState:
        evt = self._pick_event()
        if evt is None:
            raise RuntimeError("No GuessYear events are available for tournament play.")

        match_id = await self._create_match_row(
            tournament.tournament_id,
            round_number,
            bracket_position,
            player1_user_id,
            player2_user_id,
            tournament.best_of,
        )

        if not isinstance(host_channel, discord.TextChannel):
            raise RuntimeError("Tournament matches must be started from a normal text channel.")

        p1 = host_channel.guild.get_member(player1_user_id)
        p2 = host_channel.guild.get_member(player2_user_id)
        seed_message = await host_channel.send(
            f"⚔️ **Round {round_number}, Match {bracket_position}** — "
            f"{p1.mention if p1 else f'<@{player1_user_id}>'} vs {p2.mention if p2 else f'<@{player2_user_id}>'}"
        )
        thread = await host_channel.create_thread(
            name=f"duel-cup-r{round_number}-m{bracket_position}-{int(time.time()) % 10000}",
            message=seed_message,
            auto_archive_duration=60,
        )
        try:
            if p1:
                await thread.add_user(p1)
            if p2:
                await thread.add_user(p2)
        except Exception:
            pass

        match = TournamentMatchState(
            match_id=match_id,
            tournament_id=tournament.tournament_id,
            round_number=round_number,
            bracket_position=bracket_position,
            guild_id=tournament.guild_id,
            host_channel_id=tournament.channel_id,
            thread_id=thread.id,
            player1_user_id=player1_user_id,
            player2_user_id=player2_user_id,
            best_of=tournament.best_of,
            created_by_user_id=tournament.created_by_user_id,
            event_id=str(evt["id"]),
            correct_year=int(evt["year"]),
            prompt=str(evt["prompt"]),
            scores={player1_user_id: 0, player2_user_id: 0},
        )
        self._matches[match.match_id] = match
        await self._update_match_thread(match.match_id, thread.id)
        await self._post_match_question(match)
        return match

    async def _post_match_question(self, match: TournamentMatchState) -> None:
        thread = self.bot.get_channel(match.thread_id)
        guild = self.bot.get_guild(match.guild_id)
        if not isinstance(thread, discord.Thread) or guild is None:
            return
        match.round_started_at = int(time.time())
        match.round_ends_at = match.round_started_at + 60
        match.guesses.clear()

        view = TournamentMatchView(self, match)
        msg = await thread.send(embed=self._build_match_embed(match, guild), view=view)
        match.status_message_id = msg.id

        old = self._match_tasks.get(match.match_id)
        if old and not old.done():
            old.cancel()
        self._match_tasks[match.match_id] = asyncio.create_task(self._finish_question_when_ready(match.match_id))

    async def _finish_question_when_ready(self, match_id: int) -> None:
        match = self._matches.get(match_id)
        if not match or match.finished:
            return
        delay = max(0, match.round_ends_at - int(time.time()))
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        await self._resolve_match_question(match)

    async def _open_guess_modal(self, interaction: discord.Interaction, match: TournamentMatchState) -> None:
        if interaction.user.id not in {match.player1_user_id, match.player2_user_id}:
            await interaction.response.send_message("Only the two match participants can submit guesses.", ephemeral=True)
            return
        if match.finished:
            await interaction.response.send_message("This match is already finished.", ephemeral=True)
            return
        if interaction.user.id in match.guesses:
            await interaction.response.send_message("You already locked a guess for this question.", ephemeral=True)
            return
        await interaction.response.send_modal(TournamentGuessModal(self, match))

    async def _submit_hidden_guess(self, interaction: discord.Interaction, match: TournamentMatchState, raw_guess: str) -> None:
        parsed = YEAR_RE.match(raw_guess)
        if not parsed:
            await interaction.response.send_message("Enter a valid year like 1066 or 1914.", ephemeral=True)
            return
        guess_year = int(parsed.group(1))
        match.guesses[interaction.user.id] = (guess_year, int(time.time()))
        await interaction.response.send_message(f"Locked in: **{guess_year}**", ephemeral=True)
        if len(match.guesses) >= 2:
            task = self._match_tasks.get(match.match_id)
            if task and not task.done():
                task.cancel()
            await self._resolve_match_question(match)

    async def _resolve_match_question(self, match: TournamentMatchState) -> None:
        if match.finished:
            return
        thread = self.bot.get_channel(match.thread_id)
        if not isinstance(thread, discord.Thread):
            return
        p1_guess = match.guesses.get(match.player1_user_id)
        p2_guess = match.guesses.get(match.player2_user_id)
        winner_user_id: int | None = None
        winner_guess: int | None = None
        winner_diff: int | None = None

        if p1_guess and p2_guess:
            p1_diff = abs(p1_guess[0] - match.correct_year)
            p2_diff = abs(p2_guess[0] - match.correct_year)
            if p1_diff < p2_diff:
                winner_user_id = match.player1_user_id
                winner_guess = p1_guess[0]
                winner_diff = p1_diff
            elif p2_diff < p1_diff:
                winner_user_id = match.player2_user_id
                winner_guess = p2_guess[0]
                winner_diff = p2_diff
            else:
                # Equal distance: earliest guess wins.
                if p1_guess[1] <= p2_guess[1]:
                    winner_user_id = match.player1_user_id
                    winner_guess = p1_guess[0]
                    winner_diff = p1_diff
                else:
                    winner_user_id = match.player2_user_id
                    winner_guess = p2_guess[0]
                    winner_diff = p2_diff
        elif p1_guess:
            winner_user_id = match.player1_user_id
            winner_guess = p1_guess[0]
            winner_diff = abs(p1_guess[0] - match.correct_year)
        elif p2_guess:
            winner_user_id = match.player2_user_id
            winner_guess = p2_guess[0]
            winner_diff = abs(p2_guess[0] - match.correct_year)

        if winner_user_id is not None:
            match.scores[winner_user_id] = match.scores.get(winner_user_id, 0) + 1

        result = TournamentMatchResult(
            question_number=match.current_question,
            event_id=match.event_id,
            prompt=match.prompt,
            correct_year=match.correct_year,
            guesses={uid: guess for uid, (guess, _ts) in match.guesses.items()},
            winner_user_id=winner_user_id,
            winner_guess=winner_guess,
            winner_diff=winner_diff,
        )
        match.history.append(result)

        guild = self.bot.get_guild(match.guild_id)
        if guild is not None:
            p1_label = guild.get_member(match.player1_user_id).mention if guild.get_member(match.player1_user_id) else f"<@{match.player1_user_id}>"
            p2_label = guild.get_member(match.player2_user_id).mention if guild.get_member(match.player2_user_id) else f"<@{match.player2_user_id}>"
        else:
            p1_label = f"<@{match.player1_user_id}>"
            p2_label = f"<@{match.player2_user_id}>"

        lines = [
            f"**Correct year:** {match.correct_year}",
            f"{p1_label} guessed **{p1_guess[0]}**" if p1_guess else f"{p1_label} did not submit a guess.",
            f"{p2_label} guessed **{p2_guess[0]}**" if p2_guess else f"{p2_label} did not submit a guess.",
        ]
        if winner_user_id is None:
            lines.append("No point awarded this round.")
        else:
            lines.append(f"Point goes to <@{winner_user_id}> (off by {winner_diff} year(s)).")

        await thread.send("\n".join(lines))

        score1 = match.scores.get(match.player1_user_id, 0)
        score2 = match.scores.get(match.player2_user_id, 0)
        if score1 >= match.wins_needed or score2 >= match.wins_needed or match.current_question >= match.best_of:
            if score1 == score2:
                # Final fallback after best-of is exhausted.
                p1_last = p1_guess[1] if p1_guess else 10**18
                p2_last = p2_guess[1] if p2_guess else 10**18
                match.winner_user_id = match.player1_user_id if p1_last <= p2_last else match.player2_user_id
            else:
                match.winner_user_id = match.player1_user_id if score1 > score2 else match.player2_user_id
            match.finished = True
            await self._update_match_result(match)
            await self._handle_finished_match(match)
            return

        next_evt = self._pick_event()
        if next_evt is None:
            match.winner_user_id = match.player1_user_id if score1 >= score2 else match.player2_user_id
            match.finished = True
            await self._update_match_result(match)
            await self._handle_finished_match(match)
            return

        match.current_question += 1
        match.event_id = str(next_evt["id"])
        match.correct_year = int(next_evt["year"])
        match.prompt = str(next_evt["prompt"])
        await self._update_match_result(match)
        await self._post_match_question(match)

    async def _handle_finished_match(self, match: TournamentMatchState) -> None:
        tournament = self._tournaments.get(match.tournament_id)
        if tournament is None:
            return
        host_channel = self.bot.get_channel(tournament.channel_id)
        if not isinstance(host_channel, discord.TextChannel):
            return
        thread = self.bot.get_channel(match.thread_id)
        score1 = match.scores.get(match.player1_user_id, 0)
        score2 = match.scores.get(match.player2_user_id, 0)
        winner_mention = f"<@{match.winner_user_id}>" if match.winner_user_id else "No winner"
        await host_channel.send(
            f"🏁 **Round {match.round_number}, Match {match.bracket_position} finished** — "
            f"winner: {winner_mention} (`{score1}-{score2}`)"
            + (f" • Thread: {thread.mention}" if isinstance(thread, discord.Thread) else "")
        )
        if match.winner_user_id is not None:
            tournament.next_round_players.append(match.winner_user_id)
        tournament.active_match_ids.discard(match.match_id)
        if not tournament.active_match_ids:
            await self._advance_or_finish(host_channel, tournament)

    async def _advance_or_finish(self, host_channel: discord.TextChannel, tournament: TournamentState) -> None:
        players = list(tournament.next_round_players)
        if len(players) <= 1:
            tournament.status = "finished"
            tournament.winner_user_id = players[0] if players else None
            await self._set_tournament_status(tournament.tournament_id, "finished", winner_user_id=tournament.winner_user_id)
            if tournament.winner_user_id is not None:
                await host_channel.send(
                    f"🏆 **{tournament.title} champion:** <@{tournament.winner_user_id}>"
                )
            else:
                await host_channel.send(f"{tournament.title} ended without a winner.")
            return
        tournament.round_number += 1
        tournament.next_round_players = []
        await self._start_round(host_channel, tournament, players)

    @commands.group(name="dueltourney", invoke_without_command=True)
    async def dueltourney(self, ctx: commands.Context) -> None:
        await ctx.send(
            "Use `!dueltourney open`, `!dueltourney join`, `!dueltourney leave`, "
            "`!dueltourney start`, `!dueltourney status`, or `!dueltourney cancel`."
        )

    @dueltourney.command(name="open")
    async def dueltourney_open(self, ctx: commands.Context, size: int = 8, best_of: int = 3) -> None:
        if not ctx.guild or not isinstance(ctx.channel, discord.TextChannel):
            return
        if not self._can_manage(ctx.author):
            await ctx.send("Only moderators can open a duel tournament.", delete_after=10)
            return
        if not self._is_allowed_channel(ctx.channel.id):
            await ctx.send("GuessYear is not enabled in this channel.", delete_after=10)
            return
        if (ctx.guild.id, ctx.channel.id) in self._signups:
            await ctx.send("A tournament signup is already open in this channel.", delete_after=10)
            return
        size = int(size)
        if size < 2 or size > 16:
            await ctx.send("Signup cap must be between 2 and 16.", delete_after=12)
            return
        if best_of not in {1, 3, 5}:
            await ctx.send("best_of must be 1, 3, or 5.", delete_after=10)
            return
        now = int(time.time())
        title = "Friday Mini Duel Cup"
        tournament_id = await self._create_tournament_row(ctx.guild.id, ctx.channel.id, ctx.author.id, title, size, best_of, now)
        state = TournamentSignupState(
            tournament_id=tournament_id,
            guild_id=ctx.guild.id,
            channel_id=ctx.channel.id,
            created_by_user_id=ctx.author.id,
            created_at=now,
            signup_message_id=None,
            title=title,
            bracket_size=size,
            best_of=best_of,
            entrants={ctx.author.id},
        )
        await self._upsert_entry(tournament_id, ctx.author.id)
        self._signups[(ctx.guild.id, ctx.channel.id)] = state
        view = self._signup_view_for(state)
        message = await ctx.send(embed=self._build_signup_embed(state, ctx.guild), view=view)
        state.signup_message_id = message.id
        view.message = message
        await self._set_signup_message_id(tournament_id, message.id)

    @dueltourney.command(name="join")
    async def dueltourney_join(self, ctx: commands.Context) -> None:
        if not ctx.guild:
            return
        state = self._signups.get((ctx.guild.id, ctx.channel.id))
        if not state:
            await ctx.send("There is no open tournament signup in this channel.", delete_after=10)
            return
        if len(state.entrants) >= state.bracket_size and ctx.author.id not in state.entrants:
            await ctx.send("The bracket is already full.", delete_after=10)
            return
        state.entrants.add(ctx.author.id)
        await self._upsert_entry(state.tournament_id, ctx.author.id)
        await self._refresh_signup_message(state)
        await ctx.message.add_reaction("✅")

    @dueltourney.command(name="leave")
    async def dueltourney_leave(self, ctx: commands.Context) -> None:
        if not ctx.guild:
            return
        state = self._signups.get((ctx.guild.id, ctx.channel.id))
        if not state:
            await ctx.send("There is no open tournament signup in this channel.", delete_after=10)
            return
        if ctx.author.id not in state.entrants:
            await ctx.send("You are not signed up for this tournament.", delete_after=10)
            return
        state.entrants.discard(ctx.author.id)
        await self._remove_entry(state.tournament_id, ctx.author.id)
        await self._refresh_signup_message(state)
        await ctx.message.add_reaction("👋")

    @dueltourney.command(name="start")
    async def dueltourney_start(self, ctx: commands.Context) -> None:
        if not ctx.guild or not isinstance(ctx.channel, discord.TextChannel):
            return
        if not self._can_manage(ctx.author):
            await ctx.send("Only moderators can start the tournament.", delete_after=10)
            return
        state = self._signups.get((ctx.guild.id, ctx.channel.id))
        if not state:
            await ctx.send("There is no open tournament signup in this channel.", delete_after=10)
            return
        entrants = list(state.entrants)
        if len(entrants) < 2:
            await ctx.send("You need at least 2 entrants to start.", delete_after=10)
            return
        if len(entrants) % 2 != 0:
            await ctx.send(
                "You currently have an odd number of entrants. Add one more player or remove one before starting.",
                delete_after=12,
            )
            return
        random.shuffle(entrants)
        state.status = "active"
        self._signups.pop((ctx.guild.id, ctx.channel.id), None)
        actual_size = len(entrants)
        state.bracket_size = actual_size
        tournament = TournamentState(
            tournament_id=state.tournament_id,
            guild_id=state.guild_id,
            channel_id=state.channel_id,
            created_by_user_id=state.created_by_user_id,
            title=state.title,
            bracket_size=actual_size,
            best_of=state.best_of,
            signup_message_id=state.signup_message_id,
            entrants=entrants,
            round_number=1,
            status="active",
        )
        self._tournaments[tournament.tournament_id] = tournament
        await self._set_tournament_status(tournament.tournament_id, "active")
        await ctx.send(f"🏁 Starting **{state.title}** with **{len(entrants)}** entrants.")
        await self._start_round(ctx.channel, tournament, entrants)

    @dueltourney.command(name="status")
    async def dueltourney_status(self, ctx: commands.Context) -> None:
        if not ctx.guild:
            return
        signup = self._signups.get((ctx.guild.id, ctx.channel.id))
        if signup:
            await ctx.send(embed=self._build_signup_embed(signup, ctx.guild))
            return
        active = next(
            (
                t for t in self._tournaments.values()
                if t.guild_id == ctx.guild.id and t.channel_id == ctx.channel.id and t.status == "active"
            ),
            None,
        )
        if not active:
            await ctx.send("No duel tournament is currently active in this channel.", delete_after=10)
            return
        embed = discord.Embed(
            title=active.title,
            description=(
                f"**Status:** {active.status}\n"
                f"**Round:** {active.round_number}\n"
                f"**Best of:** {active.best_of}\n"
                f"**Active matches:** {len(active.active_match_ids)}"
            ),
            color=discord.Color.blurple(),
        )
        if active.next_round_players:
            embed.add_field(
                name="Already advanced",
                value=" ".join(f"<@{uid}>" for uid in active.next_round_players),
                inline=False,
            )
        await ctx.send(embed=embed)

    @dueltourney.command(name="cancel")
    async def dueltourney_cancel(self, ctx: commands.Context) -> None:
        if not ctx.guild:
            return
        if not self._can_manage(ctx.author):
            await ctx.send("Only moderators can cancel a duel tournament.", delete_after=10)
            return
        signup = self._signups.pop((ctx.guild.id, ctx.channel.id), None)
        if signup:
            signup.status = "cancelled"
            await self._set_tournament_status(signup.tournament_id, "cancelled")
            await ctx.send("Cancelled the signup.")
            return
        active = next(
            (
                t for t in self._tournaments.values()
                if t.guild_id == ctx.guild.id and t.channel_id == ctx.channel.id and t.status == "active"
            ),
            None,
        )
        if not active:
            await ctx.send("There is no active duel tournament in this channel.", delete_after=10)
            return
        active.status = "cancelled"
        await self._set_tournament_status(active.tournament_id, "cancelled")
        for match_id in list(active.active_match_ids):
            task = self._match_tasks.get(match_id)
            if task and not task.done():
                task.cancel()
        await ctx.send("Cancelled the active duel tournament.")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(DuelTournamentCog(bot))
