from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
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
    signup_window_minutes: int | None = None
    signup_deadline_at: int | None = None
    ready_deadline_at: int | None = None
    ready_confirmed: set[int] = field(default_factory=set)


@dataclass(slots=True)
class TournamentScheduleState:
    guild_id: int
    channel_id: int
    created_by_user_id: int
    title: str
    weekday: int
    hour_utc: int
    minute_utc: int
    signup_window_minutes: int
    bracket_size: int
    best_of: int
    enabled: bool = True
    last_fire_key: str | None = None


@dataclass(slots=True)
class GuildTournamentConfig:
    guild_id: int
    champion_role_id: int | None = None
    season_role_id: int | None = None
    ping_role_id: int | None = None
    reminder_minutes_before: int = 30
    signup_last_call_minutes: int = 2


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
    is_final: bool = False
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


MATCH_MESSAGE_TTL_SECONDS = 3600


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
    bracket_message_id: int | None = None
    round_pairings: dict[int, list[tuple[int, int | None]]] = field(default_factory=dict)
    round_winners: dict[int, dict[int, int | None]] = field(default_factory=dict)
    pending_matchups: list[tuple[int, int, int, bool]] = field(default_factory=list)


class TournamentGuessModal(discord.ui.Modal, title="Submit your tournament answer"):
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


class ReadyCheckView(discord.ui.View):
    def __init__(self, cog: "DuelTournamentCog", state: TournamentSignupState):
        super().__init__(timeout=None)
        self.cog = cog
        self.state = state
        self.message: discord.Message | None = None

    @discord.ui.button(label="Ready", style=discord.ButtonStyle.success, emoji="✅")
    async def ready_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog._ready_check_ready(interaction, self.state)

    @discord.ui.button(label="Drop", style=discord.ButtonStyle.danger, emoji="🚪")
    async def drop_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog._ready_check_drop(interaction, self.state)


class TournamentMatchView(discord.ui.View):
    def __init__(self, cog: "DuelTournamentCog", match: TournamentMatchState):
        super().__init__(timeout=None)
        self.cog = cog
        self.match = match

    @discord.ui.button(label="Click me to submit", style=discord.ButtonStyle.primary, emoji="🕵️")
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
        self._auto_signup_tasks: dict[tuple[int, int], asyncio.Task[Any]] = {}
        self._ready_check_tasks: dict[tuple[int, int], asyncio.Task[Any]] = {}
        self._last_call_tasks: dict[tuple[int, int], asyncio.Task[Any]] = {}
        self._schedules: dict[tuple[int, int], TournamentScheduleState] = {}
        self._guild_configs: dict[int, GuildTournamentConfig] = {}
        self._sent_announcement_keys: set[str] = set()
        self._schedule_task: asyncio.Task[Any] | None = None
        self._load_dataset()

    async def cog_load(self) -> None:
        await self._ensure_schema()
        await self._load_schedules()
        await self._load_guild_configs()
        await self._recover_runtime_signups()
        await self._recover_runtime_tournaments()
        if self._schedule_task is None:
            self._schedule_task = asyncio.create_task(self._schedule_loop())

    async def cog_unload(self) -> None:
        if self._schedule_task and not self._schedule_task.done():
            self._schedule_task.cancel()
        for task in self._match_tasks.values():
            if not task.done():
                task.cancel()
        for task in self._auto_signup_tasks.values():
            if not task.done():
                task.cancel()
        for task in self._ready_check_tasks.values():
            if not task.done():
                task.cancel()
        for task in self._last_call_tasks.values():
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

            CREATE TABLE IF NOT EXISTS guessyear_tournament_runtime (
              scope TEXT PRIMARY KEY,
              payload_json TEXT NOT NULL,
              updated_at INTEGER NOT NULL
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

            CREATE TABLE IF NOT EXISTS guessyear_tournament_schedules (
              guild_id TEXT NOT NULL,
              channel_id TEXT NOT NULL,
              created_by_user_id TEXT NOT NULL,
              title TEXT NOT NULL,
              weekday INTEGER NOT NULL,
              hour_utc INTEGER NOT NULL,
              minute_utc INTEGER NOT NULL,
              signup_window_minutes INTEGER NOT NULL DEFAULT 10,
              bracket_size INTEGER NOT NULL DEFAULT 8,
              best_of INTEGER NOT NULL DEFAULT 3,
              enabled INTEGER NOT NULL DEFAULT 1,
              last_fire_key TEXT,
              updated_at INTEGER NOT NULL,
              PRIMARY KEY (guild_id, channel_id)
            );

            CREATE INDEX IF NOT EXISTS idx_guessyear_tournament_schedules_enabled
            ON guessyear_tournament_schedules(enabled, weekday, hour_utc, minute_utc);

            CREATE TABLE IF NOT EXISTS guessyear_tournament_guild_config (
              guild_id TEXT PRIMARY KEY,
              champion_role_id TEXT,
              season_role_id TEXT,
              ping_role_id TEXT,
              reminder_minutes_before INTEGER NOT NULL DEFAULT 30,
              signup_last_call_minutes INTEGER NOT NULL DEFAULT 2,
              updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS guessyear_tournament_no_shows (
              guild_id TEXT NOT NULL,
              user_id TEXT NOT NULL,
              readycheck_missed INTEGER NOT NULL DEFAULT 0,
              match_no_shows INTEGER NOT NULL DEFAULT 0,
              inactivity_dqs INTEGER NOT NULL DEFAULT 0,
              total_flags INTEGER NOT NULL DEFAULT 0,
              last_no_show_at INTEGER,
              updated_at INTEGER NOT NULL,
              PRIMARY KEY (guild_id, user_id)
            );

            CREATE INDEX IF NOT EXISTS idx_guessyear_tournament_no_shows_rank
            ON guessyear_tournament_no_shows(guild_id, total_flags DESC, readycheck_missed DESC, match_no_shows DESC, inactivity_dqs DESC, user_id ASC);

            CREATE TABLE IF NOT EXISTS guessyear_tournament_seasons (
              season_id INTEGER PRIMARY KEY AUTOINCREMENT,
              guild_id TEXT NOT NULL,
              name TEXT NOT NULL,
              starts_at INTEGER NOT NULL,
              ends_at INTEGER NOT NULL,
              is_active INTEGER NOT NULL DEFAULT 1,
              created_by_user_id TEXT,
              created_at INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_guessyear_tournament_seasons_guild_active
            ON guessyear_tournament_seasons(guild_id, is_active, starts_at, ends_at);

            CREATE TABLE IF NOT EXISTS guessyear_tournament_season_points (
              season_id INTEGER NOT NULL,
              user_id TEXT NOT NULL,
              points INTEGER NOT NULL DEFAULT 0,
              tournaments_entered INTEGER NOT NULL DEFAULT 0,
              match_wins INTEGER NOT NULL DEFAULT 0,
              tournament_wins INTEGER NOT NULL DEFAULT 0,
              finals_reached INTEGER NOT NULL DEFAULT 0,
              updated_at INTEGER NOT NULL,
              PRIMARY KEY (season_id, user_id),
              FOREIGN KEY (season_id) REFERENCES guessyear_tournament_seasons(season_id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_guessyear_tournament_season_points_rank
            ON guessyear_tournament_season_points(season_id, points DESC, tournament_wins DESC, match_wins DESC);
            """
        )
        await self.bot.db.conn.commit()

    async def _load_schedules(self) -> None:
        if not getattr(self.bot, "db", None) or not getattr(self.bot.db, "conn", None):
            return
        cur = await self.bot.db.conn.execute(
            """
            SELECT guild_id, channel_id, created_by_user_id, title, weekday, hour_utc, minute_utc,
                   signup_window_minutes, bracket_size, best_of, enabled, last_fire_key
            FROM guessyear_tournament_schedules
            WHERE enabled=1
            """
        )
        rows = await cur.fetchall()
        self._schedules.clear()
        for row in rows:
            schedule = TournamentScheduleState(
                guild_id=int(row[0]),
                channel_id=int(row[1]),
                created_by_user_id=int(row[2]),
                title=str(row[3]),
                weekday=int(row[4]),
                hour_utc=int(row[5]),
                minute_utc=int(row[6]),
                signup_window_minutes=int(row[7]),
                bracket_size=int(row[8]),
                best_of=int(row[9]),
                enabled=bool(row[10]),
                last_fire_key=str(row[11]) if row[11] is not None else None,
            )
            self._schedules[(schedule.guild_id, schedule.channel_id)] = schedule

    async def _load_guild_configs(self) -> None:
        if not getattr(self.bot, "db", None) or not getattr(self.bot.db, "conn", None):
            return
        cur = await self.bot.db.conn.execute(
            """
            SELECT guild_id, champion_role_id, season_role_id, ping_role_id,
                   reminder_minutes_before, signup_last_call_minutes
            FROM guessyear_tournament_guild_config
            """
        )
        rows = await cur.fetchall()
        self._guild_configs.clear()
        for row in rows:
            self._guild_configs[int(row[0])] = GuildTournamentConfig(
                guild_id=int(row[0]),
                champion_role_id=int(row[1]) if row[1] is not None else None,
                season_role_id=int(row[2]) if row[2] is not None else None,
                ping_role_id=int(row[3]) if row[3] is not None else None,
                reminder_minutes_before=int(row[4] or 30),
                signup_last_call_minutes=int(row[5] or 2),
            )

    def _get_guild_config(self, guild_id: int) -> GuildTournamentConfig:
        cfg = self._guild_configs.get(guild_id)
        if cfg is None:
            cfg = GuildTournamentConfig(guild_id=guild_id)
            self._guild_configs[guild_id] = cfg
        return cfg

    async def _save_guild_config(self, config: GuildTournamentConfig) -> None:
        now = int(time.time())
        await self.bot.db.conn.execute(
            """
            INSERT INTO guessyear_tournament_guild_config (
              guild_id, champion_role_id, season_role_id, ping_role_id,
              reminder_minutes_before, signup_last_call_minutes, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
              champion_role_id=excluded.champion_role_id,
              season_role_id=excluded.season_role_id,
              ping_role_id=excluded.ping_role_id,
              reminder_minutes_before=excluded.reminder_minutes_before,
              signup_last_call_minutes=excluded.signup_last_call_minutes,
              updated_at=excluded.updated_at
            """,
            (
                str(config.guild_id),
                str(config.champion_role_id) if config.champion_role_id else None,
                str(config.season_role_id) if config.season_role_id else None,
                str(config.ping_role_id) if config.ping_role_id else None,
                int(config.reminder_minutes_before),
                int(config.signup_last_call_minutes),
                now,
            ),
        )
        await self.bot.db.conn.commit()
        self._guild_configs[config.guild_id] = config

    async def _get_no_show_stats(self, guild_id: int, user_id: int) -> dict[str, int | None]:
        cur = await self.bot.db.conn.execute(
            """
            SELECT readycheck_missed, match_no_shows, inactivity_dqs, total_flags, last_no_show_at
            FROM guessyear_tournament_no_shows
            WHERE guild_id=? AND user_id=?
            """,
            (str(guild_id), str(user_id)),
        )
        row = await cur.fetchone()
        if row is None:
            return {
                "readycheck_missed": 0,
                "match_no_shows": 0,
                "inactivity_dqs": 0,
                "total_flags": 0,
                "last_no_show_at": None,
            }
        return {
            "readycheck_missed": int(row[0] or 0),
            "match_no_shows": int(row[1] or 0),
            "inactivity_dqs": int(row[2] or 0),
            "total_flags": int(row[3] or 0),
            "last_no_show_at": int(row[4]) if row[4] is not None else None,
        }

    async def _record_no_show(
        self,
        guild_id: int,
        user_id: int,
        *,
        readycheck_missed: int = 0,
        match_no_shows: int = 0,
        inactivity_dqs: int = 0,
    ) -> None:
        total_delta = int(readycheck_missed) + int(match_no_shows) + int(inactivity_dqs)
        if total_delta <= 0:
            return
        now = int(time.time())
        await self.bot.db.conn.execute(
            """
            INSERT INTO guessyear_tournament_no_shows (
              guild_id, user_id, readycheck_missed, match_no_shows, inactivity_dqs,
              total_flags, last_no_show_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
              readycheck_missed=guessyear_tournament_no_shows.readycheck_missed + excluded.readycheck_missed,
              match_no_shows=guessyear_tournament_no_shows.match_no_shows + excluded.match_no_shows,
              inactivity_dqs=guessyear_tournament_no_shows.inactivity_dqs + excluded.inactivity_dqs,
              total_flags=guessyear_tournament_no_shows.total_flags + excluded.total_flags,
              last_no_show_at=excluded.last_no_show_at,
              updated_at=excluded.updated_at
            """,
            (
                str(guild_id),
                str(user_id),
                int(readycheck_missed),
                int(match_no_shows),
                int(inactivity_dqs),
                int(total_delta),
                now,
                now,
            ),
        )
        await self.bot.db.conn.commit()

    def _reliability_percent(self, *, cups_entered: int, total_flags: int) -> int:
        denominator = max(1, int(cups_entered) + int(total_flags))
        return max(0, min(100, round((int(cups_entered) / denominator) * 100)))

    def _match_submission_counts(self, match: TournamentMatchState) -> dict[int, int]:
        counts = {
            match.player1_user_id: 0,
            match.player2_user_id: 0,
        }
        for result in match.history:
            for user_id in result.guesses:
                counts[user_id] = counts.get(user_id, 0) + 1
        return counts

    async def _join_warning_suffix(self, guild_id: int, user_id: int) -> str:
        stats = await self._get_no_show_stats(guild_id, user_id)
        total_flags = int(stats["total_flags"] or 0)
        if total_flags <= 0:
            return ""
        reliability = self._reliability_percent(cups_entered=0, total_flags=total_flags)
        return (
            f"\n\n⚠️ You currently have **{total_flags}** no-show flag(s) "
            f"({int(stats['readycheck_missed'])} missed ready-check, "
            f"{int(stats['match_no_shows'])} match no-show, "
            f"{int(stats['inactivity_dqs'])} inactivity DQ)."
        )

    def _month_bounds(self, now: datetime) -> tuple[datetime, datetime]:
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if start.month == 12:
            end = start.replace(year=start.year + 1, month=1)
        else:
            end = start.replace(month=start.month + 1)
        return start, end

    async def _ensure_active_season(self, guild_id: int, created_by_user_id: int | None = None) -> tuple[int, str, int, int]:
        now = datetime.now(timezone.utc)
        now_ts = int(now.timestamp())
        cur = await self.bot.db.conn.execute(
            """
            SELECT season_id, name, starts_at, ends_at
            FROM guessyear_tournament_seasons
            WHERE guild_id=? AND starts_at<=? AND ends_at>?
            ORDER BY starts_at DESC
            LIMIT 1
            """,
            (str(guild_id), now_ts, now_ts),
        )
        row = await cur.fetchone()
        if row is not None:
            return int(row[0]), str(row[1]), int(row[2]), int(row[3])

        start_dt, end_dt = self._month_bounds(now)
        name = start_dt.strftime('%B %Y')
        start_ts = int(start_dt.timestamp())
        end_ts = int(end_dt.timestamp())
        cur = await self.bot.db.conn.execute(
            """
            INSERT INTO guessyear_tournament_seasons (
              guild_id, name, starts_at, ends_at, is_active, created_by_user_id, created_at
            ) VALUES (?, ?, ?, ?, 1, ?, ?)
            """,
            (
                str(guild_id),
                name,
                start_ts,
                end_ts,
                str(created_by_user_id) if created_by_user_id else None,
                now_ts,
            ),
        )
        await self.bot.db.conn.commit()
        return int(cur.lastrowid), name, start_ts, end_ts

    async def _season_standings(self, guild_id: int, limit: int = 10) -> tuple[tuple[int, str, int, int], list[tuple[int, int, int, int, int, int]]]:
        season = await self._ensure_active_season(guild_id)
        cur = await self.bot.db.conn.execute(
            """
            SELECT user_id, points, tournaments_entered, match_wins, tournament_wins, finals_reached
            FROM guessyear_tournament_season_points
            WHERE season_id=?
            ORDER BY points DESC, tournament_wins DESC, match_wins DESC, user_id ASC
            LIMIT ?
            """,
            (season[0], int(limit)),
        )
        rows = await cur.fetchall()
        parsed = [
            (int(row[0]), int(row[1]), int(row[2]), int(row[3]), int(row[4]), int(row[5]))
            for row in rows
        ]
        return season, parsed

    async def _season_add_points(
        self,
        guild_id: int,
        user_id: int,
        *,
        points: int = 0,
        tournaments_entered: int = 0,
        match_wins: int = 0,
        tournament_wins: int = 0,
        finals_reached: int = 0,
    ) -> None:
        season_id, _name, _start, _end = await self._ensure_active_season(guild_id)
        now = int(time.time())
        await self.bot.db.conn.execute(
            """
            INSERT INTO guessyear_tournament_season_points (
              season_id, user_id, points, tournaments_entered, match_wins, tournament_wins, finals_reached, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(season_id, user_id) DO UPDATE SET
              points=guessyear_tournament_season_points.points + excluded.points,
              tournaments_entered=guessyear_tournament_season_points.tournaments_entered + excluded.tournaments_entered,
              match_wins=guessyear_tournament_season_points.match_wins + excluded.match_wins,
              tournament_wins=guessyear_tournament_season_points.tournament_wins + excluded.tournament_wins,
              finals_reached=guessyear_tournament_season_points.finals_reached + excluded.finals_reached,
              updated_at=excluded.updated_at
            """,
            (
                int(season_id),
                str(user_id),
                int(points),
                int(tournaments_entered),
                int(match_wins),
                int(tournament_wins),
                int(finals_reached),
                now,
            ),
        )
        await self.bot.db.conn.commit()
        guild = self.bot.get_guild(guild_id)
        if guild is not None:
            await self._refresh_season_leader_role(guild)

    async def _award_entry_points(self, tournament: TournamentState) -> None:
        for user_id in tournament.entrants:
            await self._season_add_points(tournament.guild_id, user_id, points=1, tournaments_entered=1)

    async def _award_finalist_points(self, tournament: TournamentState, player_ids: list[int]) -> None:
        for user_id in player_ids:
            await self._season_add_points(tournament.guild_id, user_id, finals_reached=1)

    async def _refresh_season_leader_role(self, guild: discord.Guild) -> None:
        config = self._get_guild_config(guild.id)
        if not config.season_role_id:
            return
        role = guild.get_role(config.season_role_id)
        if role is None:
            return
        season, standings = await self._season_standings(guild.id, limit=50)
        leader_points = standings[0][1] if standings else None
        leader_ids = {row[0] for row in standings if leader_points is not None and row[1] == leader_points}
        current_holders = list(role.members)
        for member in current_holders:
            if member.id not in leader_ids:
                try:
                    await member.remove_roles(role, reason=f'Removed season leader role for {season[1]}')
                except Exception:
                    log.exception('Failed removing season role from %s', member.id)
        for user_id in leader_ids:
            member = guild.get_member(user_id)
            if member is not None and role not in member.roles:
                try:
                    await member.add_roles(role, reason=f'Granted season leader role for {season[1]}')
                except Exception:
                    log.exception('Failed adding season role to %s', user_id)

    async def _apply_champion_role(self, guild: discord.Guild, winner_user_id: int) -> None:
        config = self._get_guild_config(guild.id)
        if not config.champion_role_id:
            return
        role = guild.get_role(config.champion_role_id)
        if role is None:
            return
        for member in list(role.members):
            if member.id != winner_user_id:
                try:
                    await member.remove_roles(role, reason='Removed previous Friday champion role holder')
                except Exception:
                    log.exception('Failed removing champion role from %s', member.id)
        member = guild.get_member(winner_user_id)
        if member is not None and role not in member.roles:
            try:
                await member.add_roles(role, reason='Granted Friday champion role')
            except Exception:
                log.exception('Failed adding champion role to %s', winner_user_id)

    def _user_ref(self, guild: discord.Guild | None, user_id: int, *, mention: bool = True) -> str:
        member = guild.get_member(user_id) if guild is not None else None
        if member is None:
            return f'<@{user_id}>' if mention else str(user_id)
        return member.mention if mention else member.display_name

    def _role_ping_text(self, guild: discord.Guild | None, guild_id: int) -> str:
        config = self._get_guild_config(guild_id)
        if guild is None or not config.ping_role_id:
            return ''
        role = guild.get_role(config.ping_role_id)
        return f'{role.mention} ' if role is not None else ''

    def _dedupe_players(self, players: list[int]) -> list[int]:
        seen: set[int] = set()
        unique_players: list[int] = []
        for user_id in players:
            if user_id in seen:
                continue
            seen.add(user_id)
            unique_players.append(user_id)
        return unique_players

    async def _send_managed_message(
        self,
        channel: discord.abc.Messageable,
        content: str | None = None,
        *,
        embed: discord.Embed | None = None,
        view: discord.ui.View | None = None,
        delay_seconds: int = MATCH_MESSAGE_TTL_SECONDS,
    ) -> discord.Message:
        message = await channel.send(content=content, embed=embed, view=view)
        asyncio.create_task(self._delete_message_later(message, delay_seconds))
        return message

    async def _delete_message_later(self, message: discord.Message | None, delay_seconds: int = MATCH_MESSAGE_TTL_SECONDS) -> None:
        if message is None:
            return
        try:
            await asyncio.sleep(max(1, delay_seconds))
            await message.delete()
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def _upsert_schedule(
        self,
        guild_id: int,
        channel_id: int,
        created_by_user_id: int,
        title: str,
        weekday: int,
        hour_utc: int,
        minute_utc: int,
        signup_window_minutes: int,
        bracket_size: int,
        best_of: int,
    ) -> TournamentScheduleState:
        now = int(time.time())
        await self.bot.db.conn.execute(
            """
            INSERT INTO guessyear_tournament_schedules (
              guild_id, channel_id, created_by_user_id, title, weekday, hour_utc, minute_utc,
              signup_window_minutes, bracket_size, best_of, enabled, last_fire_key, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, NULL, ?)
            ON CONFLICT(guild_id, channel_id) DO UPDATE SET
              created_by_user_id=excluded.created_by_user_id,
              title=excluded.title,
              weekday=excluded.weekday,
              hour_utc=excluded.hour_utc,
              minute_utc=excluded.minute_utc,
              signup_window_minutes=excluded.signup_window_minutes,
              bracket_size=excluded.bracket_size,
              best_of=excluded.best_of,
              enabled=1,
              last_fire_key=NULL,
              updated_at=excluded.updated_at
            """,
            (
                str(guild_id),
                str(channel_id),
                str(created_by_user_id),
                title,
                int(weekday),
                int(hour_utc),
                int(minute_utc),
                int(signup_window_minutes),
                int(bracket_size),
                int(best_of),
                now,
            ),
        )
        await self.bot.db.conn.commit()
        schedule = TournamentScheduleState(
            guild_id=guild_id,
            channel_id=channel_id,
            created_by_user_id=created_by_user_id,
            title=title,
            weekday=weekday,
            hour_utc=hour_utc,
            minute_utc=minute_utc,
            signup_window_minutes=signup_window_minutes,
            bracket_size=bracket_size,
            best_of=best_of,
            enabled=True,
            last_fire_key=None,
        )
        self._schedules[(guild_id, channel_id)] = schedule
        return schedule

    async def _delete_schedule(self, guild_id: int, channel_id: int) -> None:
        await self.bot.db.conn.execute(
            "DELETE FROM guessyear_tournament_schedules WHERE guild_id=? AND channel_id=?",
            (str(guild_id), str(channel_id)),
        )
        await self.bot.db.conn.commit()
        self._schedules.pop((guild_id, channel_id), None)

    async def _set_schedule_last_fire_key(self, guild_id: int, channel_id: int, fire_key: str) -> None:
        schedule = self._schedules.get((guild_id, channel_id))
        if schedule is not None:
            schedule.last_fire_key = fire_key
        await self.bot.db.conn.execute(
            "UPDATE guessyear_tournament_schedules SET last_fire_key=?, updated_at=? WHERE guild_id=? AND channel_id=?",
            (fire_key, int(time.time()), str(guild_id), str(channel_id)),
        )
        await self.bot.db.conn.commit()

    def _weekly_fire_key(self, now: datetime, weekday: int) -> str:
        iso = now.isocalendar()
        return f"{iso.year}-W{iso.week:02d}-{weekday}"

    def _next_schedule_datetime(self, now: datetime, schedule: TournamentScheduleState) -> datetime:
        days_ahead = (schedule.weekday - now.weekday()) % 7
        target = now.replace(hour=schedule.hour_utc, minute=schedule.minute_utc, second=0, microsecond=0) + timedelta(days=days_ahead)
        if target <= now:
            target += timedelta(days=7)
        return target

    async def _maybe_send_schedule_reminder(self, schedule: TournamentScheduleState, now: datetime) -> None:
        guild = self.bot.get_guild(schedule.guild_id)
        channel = self.bot.get_channel(schedule.channel_id)
        if guild is None or not isinstance(channel, discord.TextChannel):
            return
        config = self._get_guild_config(schedule.guild_id)
        if config.reminder_minutes_before <= 0:
            return
        target = self._next_schedule_datetime(now, schedule)
        delta_seconds = int((target - now).total_seconds())
        if delta_seconds < 0 or delta_seconds > config.reminder_minutes_before * 60:
            return
        fire_key = self._weekly_fire_key(target, schedule.weekday)
        reminder_key = f'reminder:{schedule.guild_id}:{schedule.channel_id}:{fire_key}:{config.reminder_minutes_before}'
        if reminder_key in self._sent_announcement_keys:
            return
        self._sent_announcement_keys.add(reminder_key)
        ping = self._role_ping_text(guild, guild.id)
        await channel.send(
            f"{ping}⏰ **{schedule.title}** starts in about **{config.reminder_minutes_before} minute(s)** "
            f"at **{self._format_weekday(schedule.weekday)} {schedule.hour_utc:02d}:{schedule.minute_utc:02d} UTC**."
        )

    def _channel_has_live_tournament(self, guild_id: int, channel_id: int) -> bool:
        if (guild_id, channel_id) in self._signups:
            return True
        return any(
            t.guild_id == guild_id and t.channel_id == channel_id and t.status == "active"
            for t in self._tournaments.values()
        )

    async def _schedule_loop(self) -> None:
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            try:
                now = datetime.now(timezone.utc)
                for schedule in list(self._schedules.values()):
                    if not schedule.enabled:
                        continue
                    await self._maybe_send_schedule_reminder(schedule, now)
                    if now.weekday() != schedule.weekday:
                        continue
                    if now.hour != schedule.hour_utc or now.minute != schedule.minute_utc:
                        continue
                    fire_key = self._weekly_fire_key(now, schedule.weekday)
                    if schedule.last_fire_key == fire_key:
                        continue
                    if self._channel_has_live_tournament(schedule.guild_id, schedule.channel_id):
                        log.info(
                            "Skipping scheduled duel tournament for guild=%s channel=%s because one is already live",
                            schedule.guild_id,
                            schedule.channel_id,
                        )
                        continue
                    await self._set_schedule_last_fire_key(schedule.guild_id, schedule.channel_id, fire_key)
                    await self._open_scheduled_signup(schedule)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("DuelTournament schedule loop failed")
            await asyncio.sleep(20)

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

    async def _cancel_auto_signup_task(self, guild_id: int, channel_id: int) -> None:
        task = self._auto_signup_tasks.pop((guild_id, channel_id), None)
        if task and not task.done():
            task.cancel()
        last_call = self._last_call_tasks.pop((guild_id, channel_id), None)
        if last_call and not last_call.done():
            last_call.cancel()

    async def _cancel_ready_check_task(
        self,
        guild_id: int,
        channel_id: int,
        *,
        cancel_current: bool = False,
    ) -> None:
        task = self._ready_check_tasks.pop((guild_id, channel_id), None)
        if task is None or task.done():
            return
        current = asyncio.current_task()
        if task is current and not cancel_current:
            return
        task.cancel()

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

    def _runtime_scope_for_signup(self, state: TournamentSignupState) -> str:
        return f"signup:{state.guild_id}:{state.channel_id}:{state.tournament_id}"

    def _serialize_signup_state(self, state: TournamentSignupState) -> dict[str, Any]:
        return {
            "tournament_id": state.tournament_id,
            "guild_id": state.guild_id,
            "channel_id": state.channel_id,
            "created_by_user_id": state.created_by_user_id,
            "created_at": state.created_at,
            "signup_message_id": state.signup_message_id,
            "title": state.title,
            "bracket_size": state.bracket_size,
            "best_of": state.best_of,
            "entrants": sorted(state.entrants),
            "status": state.status,
            "signup_window_minutes": state.signup_window_minutes,
            "signup_deadline_at": state.signup_deadline_at,
            "ready_deadline_at": state.ready_deadline_at,
            "ready_confirmed": sorted(state.ready_confirmed),
        }

    def _deserialize_signup_state(self, payload: dict[str, Any]) -> TournamentSignupState:
        return TournamentSignupState(
            tournament_id=int(payload["tournament_id"]),
            guild_id=int(payload["guild_id"]),
            channel_id=int(payload["channel_id"]),
            created_by_user_id=int(payload["created_by_user_id"]),
            created_at=int(payload["created_at"]),
            signup_message_id=int(payload["signup_message_id"]) if payload.get("signup_message_id") is not None else None,
            title=str(payload["title"]),
            bracket_size=int(payload["bracket_size"]),
            best_of=int(payload["best_of"]),
            entrants={int(x) for x in payload.get("entrants", [])},
            status=str(payload.get("status") or "signup"),
            signup_window_minutes=int(payload["signup_window_minutes"]) if payload.get("signup_window_minutes") is not None else None,
            signup_deadline_at=int(payload["signup_deadline_at"]) if payload.get("signup_deadline_at") is not None else None,
            ready_deadline_at=int(payload["ready_deadline_at"]) if payload.get("ready_deadline_at") is not None else None,
            ready_confirmed={int(x) for x in payload.get("ready_confirmed", [])},
        )

    async def _runtime_set(self, scope: str, payload: dict[str, Any]) -> None:
        await self.bot.db.conn.execute(
            """
            INSERT INTO guessyear_tournament_runtime (scope, payload_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(scope) DO UPDATE SET payload_json=excluded.payload_json, updated_at=excluded.updated_at
            """,
            (scope, json.dumps(payload, ensure_ascii=False), int(time.time())),
        )
        await self.bot.db.conn.commit()

    async def _runtime_delete(self, scope: str) -> None:
        await self.bot.db.conn.execute(
            "DELETE FROM guessyear_tournament_runtime WHERE scope=?",
            (scope,),
        )
        await self.bot.db.conn.commit()

    async def _save_signup_runtime(self, state: TournamentSignupState) -> None:
        await self._runtime_set(self._runtime_scope_for_signup(state), self._serialize_signup_state(state))

    async def _delete_signup_runtime(self, state: TournamentSignupState) -> None:
        await self._runtime_delete(self._runtime_scope_for_signup(state))

    def _runtime_scope_for_tournament(self, tournament: TournamentState | int) -> str:
        tournament_id = tournament if isinstance(tournament, int) else tournament.tournament_id
        return f"tournament:{tournament_id}"

    def _runtime_scope_for_match(self, match: TournamentMatchState | int) -> str:
        match_id = match if isinstance(match, int) else match.match_id
        return f"match:{match_id}"

    def _serialize_tournament_state(self, tournament: TournamentState) -> dict[str, Any]:
        return {
            "tournament_id": tournament.tournament_id,
            "guild_id": tournament.guild_id,
            "channel_id": tournament.channel_id,
            "created_by_user_id": tournament.created_by_user_id,
            "title": tournament.title,
            "bracket_size": tournament.bracket_size,
            "best_of": tournament.best_of,
            "signup_message_id": tournament.signup_message_id,
            "entrants": list(tournament.entrants),
            "round_number": tournament.round_number,
            "active_match_ids": sorted(tournament.active_match_ids),
            "next_round_players": list(tournament.next_round_players),
            "status": tournament.status,
            "winner_user_id": tournament.winner_user_id,
            "bracket_message_id": tournament.bracket_message_id,
            "round_pairings": {
                str(round_number): [[player1, player2] for player1, player2 in pairings]
                for round_number, pairings in tournament.round_pairings.items()
            },
            "round_winners": {
                str(round_number): {str(position): winner for position, winner in winners.items()}
                for round_number, winners in tournament.round_winners.items()
            },
            "pending_matchups": [list(item) for item in tournament.pending_matchups],
        }

    def _deserialize_tournament_state(self, payload: dict[str, Any]) -> TournamentState:
        return TournamentState(
            tournament_id=int(payload["tournament_id"]),
            guild_id=int(payload["guild_id"]),
            channel_id=int(payload["channel_id"]),
            created_by_user_id=int(payload["created_by_user_id"]),
            title=str(payload["title"]),
            bracket_size=int(payload["bracket_size"]),
            best_of=int(payload["best_of"]),
            signup_message_id=int(payload["signup_message_id"]) if payload.get("signup_message_id") is not None else None,
            entrants=[int(x) for x in payload.get("entrants", [])],
            round_number=int(payload.get("round_number") or 0),
            active_match_ids={int(x) for x in payload.get("active_match_ids", [])},
            next_round_players=[int(x) for x in payload.get("next_round_players", [])],
            status=str(payload.get("status") or "signup"),
            winner_user_id=int(payload["winner_user_id"]) if payload.get("winner_user_id") is not None else None,
            bracket_message_id=int(payload["bracket_message_id"]) if payload.get("bracket_message_id") is not None else None,
            round_pairings={
                int(round_number): [
                    (int(pair[0]), int(pair[1]) if pair[1] is not None else None)
                    for pair in pairings
                ]
                for round_number, pairings in (payload.get("round_pairings") or {}).items()
            },
            round_winners={
                int(round_number): {
                    int(position): (int(winner) if winner is not None else None)
                    for position, winner in winners.items()
                }
                for round_number, winners in (payload.get("round_winners") or {}).items()
            },
            pending_matchups=[
                (int(item[0]), int(item[1]), int(item[2]), bool(item[3]))
                for item in payload.get("pending_matchups", [])
            ],
        )

    def _serialize_match_result(self, result: TournamentMatchResult) -> dict[str, Any]:
        return {
            "question_number": result.question_number,
            "event_id": result.event_id,
            "prompt": result.prompt,
            "correct_year": result.correct_year,
            "guesses": {str(user_id): guess for user_id, guess in result.guesses.items()},
            "winner_user_id": result.winner_user_id,
            "winner_guess": result.winner_guess,
            "winner_diff": result.winner_diff,
        }

    def _deserialize_match_result(self, payload: dict[str, Any]) -> TournamentMatchResult:
        return TournamentMatchResult(
            question_number=int(payload["question_number"]),
            event_id=str(payload["event_id"]),
            prompt=str(payload["prompt"]),
            correct_year=int(payload["correct_year"]),
            guesses={int(user_id): int(guess) for user_id, guess in (payload.get("guesses") or {}).items()},
            winner_user_id=int(payload["winner_user_id"]) if payload.get("winner_user_id") is not None else None,
            winner_guess=int(payload["winner_guess"]) if payload.get("winner_guess") is not None else None,
            winner_diff=int(payload["winner_diff"]) if payload.get("winner_diff") is not None else None,
        )

    def _serialize_match_state(self, match: TournamentMatchState) -> dict[str, Any]:
        return {
            "match_id": match.match_id,
            "tournament_id": match.tournament_id,
            "round_number": match.round_number,
            "bracket_position": match.bracket_position,
            "guild_id": match.guild_id,
            "host_channel_id": match.host_channel_id,
            "thread_id": match.thread_id,
            "player1_user_id": match.player1_user_id,
            "player2_user_id": match.player2_user_id,
            "best_of": match.best_of,
            "created_by_user_id": match.created_by_user_id,
            "event_id": match.event_id,
            "correct_year": match.correct_year,
            "prompt": match.prompt,
            "is_final": match.is_final,
            "current_question": match.current_question,
            "round_started_at": match.round_started_at,
            "round_ends_at": match.round_ends_at,
            "scores": {str(user_id): score for user_id, score in match.scores.items()},
            "guesses": {str(user_id): [guess, guess_ts] for user_id, (guess, guess_ts) in match.guesses.items()},
            "history": [self._serialize_match_result(result) for result in match.history],
            "finished": match.finished,
            "winner_user_id": match.winner_user_id,
            "status_message_id": match.status_message_id,
        }

    def _deserialize_match_state(self, payload: dict[str, Any]) -> TournamentMatchState:
        return TournamentMatchState(
            match_id=int(payload["match_id"]),
            tournament_id=int(payload["tournament_id"]),
            round_number=int(payload["round_number"]),
            bracket_position=int(payload["bracket_position"]),
            guild_id=int(payload["guild_id"]),
            host_channel_id=int(payload["host_channel_id"]),
            thread_id=int(payload["thread_id"]),
            player1_user_id=int(payload["player1_user_id"]),
            player2_user_id=int(payload["player2_user_id"]),
            best_of=int(payload["best_of"]),
            created_by_user_id=int(payload["created_by_user_id"]),
            event_id=str(payload["event_id"]),
            correct_year=int(payload["correct_year"]),
            prompt=str(payload["prompt"]),
            is_final=bool(payload.get("is_final", False)),
            current_question=int(payload.get("current_question") or 1),
            round_started_at=int(payload.get("round_started_at") or 0),
            round_ends_at=int(payload.get("round_ends_at") or 0),
            scores={int(user_id): int(score) for user_id, score in (payload.get("scores") or {}).items()},
            guesses={int(user_id): (int(values[0]), int(values[1])) for user_id, values in (payload.get("guesses") or {}).items()},
            history=[self._deserialize_match_result(item) for item in payload.get("history", [])],
            finished=bool(payload.get("finished", False)),
            winner_user_id=int(payload["winner_user_id"]) if payload.get("winner_user_id") is not None else None,
            status_message_id=int(payload["status_message_id"]) if payload.get("status_message_id") is not None else None,
        )

    async def _save_tournament_runtime(self, tournament: TournamentState) -> None:
        await self._runtime_set(self._runtime_scope_for_tournament(tournament), self._serialize_tournament_state(tournament))

    async def _delete_tournament_runtime(self, tournament: TournamentState | int) -> None:
        await self._runtime_delete(self._runtime_scope_for_tournament(tournament))

    async def _save_match_runtime(self, match: TournamentMatchState) -> None:
        await self._runtime_set(self._runtime_scope_for_match(match), self._serialize_match_state(match))

    async def _delete_match_runtime(self, match: TournamentMatchState | int) -> None:
        await self._runtime_delete(self._runtime_scope_for_match(match))

    async def _recover_runtime_signups(self) -> None:
        if not getattr(self.bot, "db", None) or not getattr(self.bot.db, "conn", None):
            return
        cur = await self.bot.db.conn.execute(
            "SELECT scope, payload_json FROM guessyear_tournament_runtime WHERE scope LIKE 'signup:%'"
        )
        rows = await cur.fetchall()
        now = int(time.time())
        for scope, payload_json in rows:
            try:
                payload = json.loads(str(payload_json))
                state = self._deserialize_signup_state(payload)
            except Exception:
                log.exception("Failed to recover signup runtime for %s", scope)
                continue
            if state.status in {"cancelled", "active", "finished"}:
                await self._runtime_delete(str(scope))
                continue
            self._signups[(state.guild_id, state.channel_id)] = state
            if state.status == "signup" and state.signup_deadline_at and state.signup_deadline_at > now:
                self._auto_signup_tasks[(state.guild_id, state.channel_id)] = asyncio.create_task(
                    self._auto_start_signup_after_delay(state.guild_id, state.channel_id, state.signup_deadline_at - now)
                )
            elif state.status == "ready_check":
                if state.ready_deadline_at and state.ready_deadline_at > now:
                    self._ready_check_tasks[(state.guild_id, state.channel_id)] = asyncio.create_task(
                        self._finalize_ready_check_after_delay(state.guild_id, state.channel_id, state.ready_deadline_at - now)
                    )
                    for user_id in state.entrants:
                        if user_id not in state.ready_confirmed:
                            asyncio.create_task(self._send_ready_dm(state, user_id, recovered=True))
                else:
                    asyncio.create_task(self._finalize_ready_check(state))
        if rows:
            log.info("Recovered %d duel tournament signup/ready-check state(s)", len(rows))
            for state in list(self._signups.values()):
                asyncio.create_task(self._restore_signup_message_after_ready(state))


    async def _recover_runtime_tournaments(self) -> None:
        if not getattr(self.bot, "db", None) or not getattr(self.bot.db, "conn", None):
            return

        cur = await self.bot.db.conn.execute(
            "SELECT scope, payload_json FROM guessyear_tournament_runtime WHERE scope LIKE 'tournament:%'"
        )
        tournament_rows = await cur.fetchall()
        recovered_tournaments = 0
        for scope, payload_json in tournament_rows:
            try:
                payload = json.loads(str(payload_json))
                tournament = self._deserialize_tournament_state(payload)
            except Exception:
                log.exception("Failed to recover tournament runtime for %s", scope)
                continue
            if tournament.status not in {"active", "paused"}:
                await self._runtime_delete(str(scope))
                continue
            self._tournaments[tournament.tournament_id] = tournament
            recovered_tournaments += 1

        cur = await self.bot.db.conn.execute(
            "SELECT scope, payload_json FROM guessyear_tournament_runtime WHERE scope LIKE 'match:%'"
        )
        match_rows = await cur.fetchall()
        recovered_matches = 0
        for scope, payload_json in match_rows:
            try:
                payload = json.loads(str(payload_json))
                match = self._deserialize_match_state(payload)
            except Exception:
                log.exception("Failed to recover match runtime for %s", scope)
                continue
            if match.finished:
                await self._runtime_delete(str(scope))
                continue
            tournament = self._tournaments.get(match.tournament_id)
            if tournament is None:
                await self._runtime_delete(str(scope))
                continue
            self._matches[match.match_id] = match
            tournament.active_match_ids.add(match.match_id)
            recovered_matches += 1

        if recovered_tournaments or recovered_matches:
            log.info(
                "Recovered %d active duel tournament(s) and %d in-progress match(es)",
                recovered_tournaments,
                recovered_matches,
            )
            for tournament_id in list(self._tournaments):
                asyncio.create_task(self._restore_tournament_after_ready(tournament_id))

    async def _restore_signup_message_after_ready(self, state: TournamentSignupState) -> None:
        await self.bot.wait_until_ready()
        try:
            await self._refresh_signup_message(state)
        except Exception:
            channel = self.bot.get_channel(state.channel_id)
            if isinstance(channel, discord.TextChannel):
                try:
                    view = self._signup_view_for(state)
                    message = await channel.send(
                        embed=self._build_signup_embed(state, channel.guild),
                        view=view,
                    )
                    state.signup_message_id = message.id
                    await self._set_signup_message_id(state.tournament_id, message.id)
                    await self._save_signup_runtime(state)
                except Exception:
                    log.exception("Failed to restore signup/ready-check message for tournament %s", state.tournament_id)

    async def _resolve_channel(self, channel_id: int) -> discord.abc.GuildChannel | discord.Thread | discord.abc.PrivateChannel | None:
        channel = self.bot.get_channel(channel_id)
        if channel is not None:
            return channel
        try:
            return await self.bot.fetch_channel(channel_id)
        except Exception:
            return None

    async def _restore_tournament_after_ready(self, tournament_id: int) -> None:
        await self.bot.wait_until_ready()
        tournament = self._tournaments.get(tournament_id)
        if tournament is None or tournament.status not in {"active", "paused"}:
            return
        host_channel = await self._resolve_channel(tournament.channel_id)
        if not isinstance(host_channel, discord.TextChannel):
            log.warning("Could not restore tournament %s because host channel %s is unavailable", tournament_id, tournament.channel_id)
            return

        try:
            await self._refresh_bracket_message(host_channel, tournament)
        except Exception:
            log.exception("Failed to refresh bracket board during tournament recovery for %s", tournament_id)

        is_paused = tournament.status == "paused"
        for match_id in list(tournament.active_match_ids):
            match = self._matches.get(match_id)
            if match is None:
                tournament.active_match_ids.discard(match_id)
                continue
            try:
                await self._restore_match_after_ready(match, paused=is_paused)
            except Exception:
                log.exception("Failed to restore duel tournament match %s", match_id)

        await self._save_tournament_runtime(tournament)
        if not tournament.active_match_ids:
            await self._advance_or_finish(host_channel, tournament)

    async def _restore_match_after_ready(self, match: TournamentMatchState, *, paused: bool = False) -> None:
        thread = await self._ensure_match_thread(match, create_if_missing=not paused)
        guild = self.bot.get_guild(match.guild_id)
        if not isinstance(thread, discord.Thread) or guild is None:
            log.warning("Could not restore match %s because thread %s or guild %s is unavailable", match.match_id, match.thread_id, match.guild_id)
            return

        await self._edit_match_message(match, disabled=paused, notice="🔄 Recovered after restart.")
        await self._save_match_runtime(match)

        old = self._match_tasks.get(match.match_id)
        if old and not old.done():
            old.cancel()
        if match.finished or paused:
            return
        now = int(time.time())
        if len(match.guesses) >= 2 or match.round_ends_at <= now:
            self._match_tasks[match.match_id] = asyncio.create_task(self._resolve_match_question(match))
        else:
            self._match_tasks[match.match_id] = asyncio.create_task(self._finish_question_when_ready(match.match_id))

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

    async def _open_signup_internal(
        self,
        channel: discord.TextChannel,
        *,
        created_by_user_id: int,
        title: str,
        bracket_size: int,
        best_of: int,
        auto_join_user_id: int | None = None,
        signup_window_minutes: int | None = None,
    ) -> TournamentSignupState:
        now = int(time.time())
        tournament_id = await self._create_tournament_row(
            channel.guild.id,
            channel.id,
            created_by_user_id,
            title,
            bracket_size,
            best_of,
            now,
        )
        state = TournamentSignupState(
            tournament_id=tournament_id,
            guild_id=channel.guild.id,
            channel_id=channel.id,
            created_by_user_id=created_by_user_id,
            created_at=now,
            signup_message_id=None,
            title=title,
            bracket_size=bracket_size,
            best_of=best_of,
            entrants={auto_join_user_id} if auto_join_user_id is not None else set(),
            signup_window_minutes=signup_window_minutes,
            signup_deadline_at=(now + signup_window_minutes * 60) if signup_window_minutes else None,
        )
        if auto_join_user_id is not None:
            await self._upsert_entry(tournament_id, auto_join_user_id)
        self._signups[(channel.guild.id, channel.id)] = state
        view = self._signup_view_for(state)
        message = await channel.send(embed=self._build_signup_embed(state, channel.guild), view=view)
        state.signup_message_id = message.id
        view.message = message
        await self._set_signup_message_id(tournament_id, message.id)
        await self._save_signup_runtime(state)

        if signup_window_minutes:
            await self._cancel_auto_signup_task(channel.guild.id, channel.id)
            self._auto_signup_tasks[(channel.guild.id, channel.id)] = asyncio.create_task(
                self._auto_start_signup_after_delay(state.guild_id, state.channel_id, signup_window_minutes * 60)
            )
        return state

    async def _open_scheduled_signup(self, schedule: TournamentScheduleState) -> None:
        channel = self.bot.get_channel(schedule.channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        bot_user_id = self.bot.user.id if self.bot.user else schedule.created_by_user_id
        state = await self._open_signup_internal(
            channel,
            created_by_user_id=bot_user_id,
            title=schedule.title,
            bracket_size=schedule.bracket_size,
            best_of=schedule.best_of,
            auto_join_user_id=None,
            signup_window_minutes=schedule.signup_window_minutes,
        )
        guild = channel.guild
        ping = self._role_ping_text(guild, guild.id)
        await self._send_managed_message(
            channel,
            f"{ping}📣 **Scheduled duel tournament signup is open.** "
            f"Join within **{schedule.signup_window_minutes} minute(s)**. "
            "After signup closes, the bot will run a DM-backed ready check and then start with any confirmed count of 2 or more."
        )
        config = self._get_guild_config(guild.id)
        last_call_minutes = max(0, min(config.signup_last_call_minutes, schedule.signup_window_minutes - 1))
        if last_call_minutes > 0:
            self._last_call_tasks[(guild.id, channel.id)] = asyncio.create_task(
                self._send_signup_last_call_after_delay(guild.id, channel.id, (schedule.signup_window_minutes - last_call_minutes) * 60, last_call_minutes)
            )
        await self._refresh_signup_message(state)

    async def _auto_start_signup_after_delay(self, guild_id: int, channel_id: int, delay_seconds: int) -> None:
        try:
            await asyncio.sleep(max(1, delay_seconds))
        except asyncio.CancelledError:
            return
        state = self._signups.get((guild_id, channel_id))
        if state is None or state.status != "signup":
            return
        channel = self.bot.get_channel(channel_id)
        guild = self.bot.get_guild(guild_id)
        if not isinstance(channel, discord.TextChannel) or guild is None:
            return
        entrants = list(state.entrants)
        if len(entrants) >= 2:
            notice = await self._send_managed_message(channel,
                f"⏳ Signup closed for **{state.title}**. Starting a **2-minute ready check** now."
            )
            await self._begin_ready_check(channel, state)
            self._auto_signup_tasks.pop((guild_id, channel_id), None)
            self._last_call_tasks.pop((guild_id, channel_id), None)
            return

        self._signups.pop((guild_id, channel_id), None)
        state.status = "cancelled"
        await self._set_tournament_status(state.tournament_id, "cancelled")
        await self._disable_signup_message(state, guild, new_title=f"{state.title} • Cancelled")
        await self._delete_signup_runtime(state)
        await channel.send("❌ Scheduled tournament cancelled. Not enough players joined before the signup window closed.")
        self._auto_signup_tasks.pop((guild_id, channel_id), None)

    async def _send_signup_last_call_after_delay(self, guild_id: int, channel_id: int, delay_seconds: int, minutes_left: int) -> None:
        try:
            await asyncio.sleep(max(1, delay_seconds))
        except asyncio.CancelledError:
            return
        state = self._signups.get((guild_id, channel_id))
        channel = self.bot.get_channel(channel_id)
        guild = self.bot.get_guild(guild_id)
        if state is None or state.status != 'signup' or not isinstance(channel, discord.TextChannel) or guild is None:
            return
        ping = self._role_ping_text(guild, guild.id)
        await self._send_managed_message(
            channel,
            f"{ping}⏳ **Last call** — **{state.title}** closes in about **{minutes_left} minute(s)**. "
            f"Current entrants: **{len(state.entrants)}**."
        )
        self._last_call_tasks.pop((guild_id, channel_id), None)

    async def _begin_ready_check(self, channel: discord.TextChannel, state: TournamentSignupState) -> None:
        if state.status != "signup":
            return
        state.status = "ready_check"
        state.signup_deadline_at = None
        state.ready_deadline_at = int(time.time()) + 120
        state.ready_confirmed.clear()
        await self._set_tournament_status(state.tournament_id, "ready_check")
        await self._save_signup_runtime(state)
        await self._refresh_signup_message(state)
        for user_id in list(state.entrants):
            await self._send_ready_dm(state, user_id)
        await self._cancel_ready_check_task(state.guild_id, state.channel_id)
        self._ready_check_tasks[(state.guild_id, state.channel_id)] = asyncio.create_task(
            self._finalize_ready_check_after_delay(state.guild_id, state.channel_id, 120)
        )
        await self._send_managed_message(
            channel,
            "📨 **Ready check started.** Please confirm within **2 minutes** using the button below, "
            "`!dueltourney ready`, or the DM the bot just sent you."
        )

    async def _send_ready_dm(self, state: TournamentSignupState, user_id: int, *, recovered: bool = False) -> None:
        user = self.bot.get_user(user_id)
        if user is None:
            try:
                user = await self.bot.fetch_user(user_id)
            except Exception:
                return
        guild = self.bot.get_guild(state.guild_id)
        channel = self.bot.get_channel(state.channel_id)
        channel_hint = channel.mention if isinstance(channel, discord.TextChannel) else f"channel {state.channel_id}"
        prefix = "[Recovered] " if recovered else ""
        try:
            await user.send(
                f"{prefix}Ready check for **{state.title}** in **{guild.name if guild else state.guild_id}**.\n"
                f"Please confirm within 2 minutes. Use **!dueltourney ready** here in DM, or in {channel_hint}.\n"
                f"If you cannot play, use **!dueltourney drop**."
            )
        except Exception:
            log.info("Could not DM ready check to user %s", user_id)

    async def _finalize_ready_check_after_delay(self, guild_id: int, channel_id: int, delay_seconds: int) -> None:
        try:
            await asyncio.sleep(max(1, delay_seconds))
        except asyncio.CancelledError:
            return
        state = self._signups.get((guild_id, channel_id))
        if state is None or state.status != "ready_check":
            return
        await self._finalize_ready_check(state)

    async def _finalize_ready_check(self, state: TournamentSignupState) -> None:
        channel = await self._resolve_channel(state.channel_id)
        guild = self.bot.get_guild(state.guild_id)
        if not isinstance(channel, discord.TextChannel) or guild is None:
            return
        confirmed = [uid for uid in state.entrants if uid in state.ready_confirmed]
        unconfirmed = [uid for uid in state.entrants if uid not in state.ready_confirmed]
        await self._cancel_ready_check_task(state.guild_id, state.channel_id)

        if unconfirmed:
            for uid in unconfirmed:
                await self._record_no_show(state.guild_id, uid, readycheck_missed=1)

        if len(confirmed) < 2:
            for uid in unconfirmed:
                await self._remove_entry(state.tournament_id, uid)
            self._signups.pop((state.guild_id, state.channel_id), None)
            state.status = "cancelled"
            await self._set_tournament_status(state.tournament_id, "cancelled")
            await self._disable_signup_message(state, guild, new_title=f"{state.title} • Cancelled")
            await self._delete_signup_runtime(state)
            if unconfirmed:
                await self._send_managed_message(
                    channel,
                    "❌ Tournament cancelled. Fewer than 2 players confirmed during ready check. "
                    f"Removed {len(unconfirmed)} unready player(s)."
                )
            else:
                await self._send_managed_message(channel, "❌ Tournament cancelled. Fewer than 2 players confirmed during ready check.")
            return

        for uid in unconfirmed:
            await self._remove_entry(state.tournament_id, uid)
        entrants = list(confirmed)
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
        await self._award_entry_points(tournament)
        await self._disable_signup_message(state, guild, new_title=f"{state.title} • Started")
        await self._delete_signup_runtime(state)
        removed_suffix = f" Removed **{len(unconfirmed)}** unready player(s)." if unconfirmed else ""
        await self._send_managed_message(
            channel,
            f"🏁 Starting **{state.title}** with **{len(entrants)}** confirmed entrant(s)."
            f"{removed_suffix} Byes will be assigned automatically if needed."
        )
        await self._start_round(channel, tournament, entrants)

    async def _mark_ready(self, state: TournamentSignupState, user_id: int) -> tuple[bool, str]:
        if state.status != "ready_check":
            return False, "Ready check is not active right now."
        if user_id not in state.entrants:
            return False, "You are not entered in this tournament."
        if user_id in state.ready_confirmed:
            return False, "You are already marked ready."
        state.ready_confirmed.add(user_id)
        await self._save_signup_runtime(state)
        await self._refresh_signup_message(state)
        if state.ready_confirmed == state.entrants and len(state.ready_confirmed) >= 2:
            await self._finalize_ready_check(state)
            return True, "Everyone is ready. Starting the tournament now."
        return True, "You are marked ready."

    async def _drop_ready_participant(self, state: TournamentSignupState, user_id: int) -> tuple[bool, str]:
        if state.status != "ready_check":
            return False, "Ready check is not active right now."
        if user_id not in state.entrants:
            return False, "You are not entered in this tournament."
        state.entrants.discard(user_id)
        state.ready_confirmed.discard(user_id)
        await self._remove_entry(state.tournament_id, user_id)
        await self._save_signup_runtime(state)
        await self._refresh_signup_message(state)
        if len(state.entrants) < 2:
            await self._finalize_ready_check(state)
            return True, "You dropped from the tournament."
        if state.ready_confirmed == state.entrants and len(state.ready_confirmed) >= 2:
            await self._finalize_ready_check(state)
            return True, "You dropped from the tournament. Everyone else is ready, so it is starting now."
        return True, "You dropped from the tournament."

    async def _ready_check_ready(self, interaction: discord.Interaction, state: TournamentSignupState) -> None:
        ok, message = await self._mark_ready(state, interaction.user.id)
        await interaction.response.send_message(message, ephemeral=True)

    async def _ready_check_drop(self, interaction: discord.Interaction, state: TournamentSignupState) -> None:
        ok, message = await self._drop_ready_participant(state, interaction.user.id)
        await interaction.response.send_message(message, ephemeral=True)

    async def _disable_signup_message(self, state: TournamentSignupState, guild: discord.Guild, *, new_title: str | None = None) -> None:
        channel = self.bot.get_channel(state.channel_id)
        if not isinstance(channel, discord.TextChannel) or state.signup_message_id is None:
            return
        try:
            msg = await channel.fetch_message(state.signup_message_id)
        except Exception:
            return
        view = self._signup_view_for(state)
        for child in view.children:
            child.disabled = True
        embed = self._build_signup_embed(state, guild)
        if new_title:
            embed.title = new_title
        if state.status == "cancelled":
            embed.color = discord.Color.dark_grey()
        await msg.edit(embed=embed, view=view)

    def _build_signup_embed(self, state: TournamentSignupState, guild: discord.Guild) -> discord.Embed:
        entrant_lines: list[str] = []
        for uid in sorted(state.entrants):
            member = guild.get_member(uid)
            if state.status == "ready_check":
                marker = "✅" if uid in state.ready_confirmed else "⏳"
            else:
                marker = "•"
            if member is not None:
                entrant_lines.append(f"{marker} {member.display_name} ({member.mention})")
            else:
                entrant_lines.append(f"{marker} <@{uid}>")

        if state.status == "ready_check":
            description = (
                "Ready check is live. Confirm now so the bracket can start.\n\n"
                f"**Confirmed:** {len(state.ready_confirmed)}/{len(state.entrants)}\n"
                f"**Match format:** best of {state.best_of}\n"
                + (f"**Deadline:** <t:{state.ready_deadline_at}:R>\n" if state.ready_deadline_at else "")
                + "**DMs sent:** yes, with channel fallback if a DM cannot be delivered.\n"
                + "**Bracket rule:** the tournament starts with any confirmed player count from 2 to 16, with byes assigned automatically when needed."
            )
            title = f"{state.title} • Ready Check"
            footer = "Use Ready / Drop below, or !dueltourney ready / !dueltourney drop."
            color = discord.Color.orange()
            field_name = f"Players ({len(state.entrants)}/{state.bracket_size})"
        else:
            if state.status == "active":
                description = (
                    "Signup is closed and the tournament is being created now.\n\n"
                    f"**Entrants locked:** {len(state.entrants)}\n"
                    f"**Match format:** best of {state.best_of}\n"
                    + "**Bracket rule:** byes are assigned automatically when needed.\n"
                    + "**Next step:** the first match thread should appear shortly."
                )
                footer = "Tournament is starting."
                color = discord.Color.green()
            elif state.status == "cancelled":
                description = "This tournament is no longer active."
                footer = "Tournament cancelled."
                color = discord.Color.dark_grey()
            else:
                description = (
                    "Friday mini cup signups are open. Join now and the bot will seed a single-elimination bracket.\n\n"
                    f"**Signup cap:** {state.bracket_size}\n"
                    f"**Match format:** best of {state.best_of}\n"
                    + (f"**Signup window:** {state.signup_window_minutes} minute(s)\n" if state.signup_window_minutes else "")
                    + (f"**Auto-ready-check:** <t:{state.signup_deadline_at}:R>\n" if state.signup_deadline_at else "")
                    + "**Start rule:** the tournament can start with 2 or more confirmed players. Byes are assigned automatically when needed.\n"
                    + "**Tiebreaker:** if both guesses are equally close, the earlier guess wins the point."
                )
                footer = "Use the buttons below or !dueltourney join / !dueltourney leave."
                color = discord.Color.gold()
            title = state.title
            field_name = f"Entrants ({len(state.entrants)}/{state.bracket_size})"

        embed = discord.Embed(title=title, description=description, color=color)
        embed.add_field(name=field_name, value="\n".join(entrant_lines) if entrant_lines else "No entrants yet.", inline=False)
        embed.set_footer(text=footer)
        return embed

    def _build_match_embed(self, match: TournamentMatchState, guild: discord.Guild) -> discord.Embed:
        p1 = guild.get_member(match.player1_user_id)
        p2 = guild.get_member(match.player2_user_id)
        s1 = match.scores.get(match.player1_user_id, 0)
        s2 = match.scores.get(match.player2_user_id, 0)
        round_title = 'Final' if match.is_final else f'Round {match.round_number}'
        embed = discord.Embed(
            title=f"Tournament Match • {round_title}",
            description=match.prompt,
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Players", value=f"{p1.mention if p1 else f'<@{match.player1_user_id}>'} vs {p2.mention if p2 else f'<@{match.player2_user_id}>'}", inline=False)
        embed.add_field(name="Score", value=f"{s1} - {s2}", inline=True)
        embed.add_field(name="Question", value=f"{match.current_question}/{match.best_of}", inline=True)
        embed.add_field(name="How to play", value="Press **Submit your answer** and enter the year.", inline=False)
        embed.set_footer(text="Earliest guess wins equal-distance ties.")
        return embed
    def _build_match_view(self, match: TournamentMatchState, *, disabled: bool = False) -> TournamentMatchView:
        view = TournamentMatchView(self, match)
        if disabled:
            for child in view.children:
                child.disabled = True
        return view

    async def _ensure_match_thread(self, match: TournamentMatchState, *, create_if_missing: bool = True) -> discord.Thread | None:
        thread = await self._resolve_channel(match.thread_id)
        if isinstance(thread, discord.Thread):
            return thread
        if not create_if_missing:
            return None
        host_channel = await self._resolve_channel(match.host_channel_id)
        if not isinstance(host_channel, discord.TextChannel):
            return None
        p1 = host_channel.guild.get_member(match.player1_user_id)
        p2 = host_channel.guild.get_member(match.player2_user_id)
        seed_message = await self._send_managed_message(host_channel,
            f"🔁 **Recovered Match Thread** — "
            f"{p1.mention if p1 else f'<@{match.player1_user_id}>'} vs {p2.mention if p2 else f'<@{match.player2_user_id}>'}"
        )
        thread = await host_channel.create_thread(
            name=f"duel-cup-r{match.round_number}-m{match.bracket_position}-recovered-{int(time.time()) % 10000}",
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
        match.thread_id = thread.id
        await self._update_match_thread(match.match_id, thread.id)
        await self._save_match_runtime(match)
        return thread

    async def _edit_match_message(
        self,
        match: TournamentMatchState,
        *,
        disabled: bool,
        notice: str | None = None,
    ) -> discord.Message | None:
        thread = await self._ensure_match_thread(match, create_if_missing=not disabled)
        guild = self.bot.get_guild(match.guild_id)
        if not isinstance(thread, discord.Thread) or guild is None:
            return None
        embed = self._build_match_embed(match, guild)
        if disabled:
            embed.title = f"{embed.title} • Paused"
            embed.set_footer(text="Paused by a moderator. Wait for resume.")
        view = self._build_match_view(match, disabled=disabled)
        message: discord.Message | None = None
        if match.status_message_id is not None:
            try:
                message = await thread.fetch_message(match.status_message_id)
                await message.edit(content=notice or None, embed=embed, view=view)
            except Exception:
                message = None
        if message is None:
            message = await thread.send(content=notice, embed=embed, view=view)
            match.status_message_id = message.id
        return message

    def _get_live_tournament(self, guild_id: int, channel_id: int) -> TournamentState | None:
        return next(
            (
                t for t in self._tournaments.values()
                if t.guild_id == guild_id and t.channel_id == channel_id and t.status in {"active", "paused"}
            ),
            None,
        )

    def _match_for_thread(self, tournament: TournamentState, thread_id: int) -> TournamentMatchState | None:
        for match_id in tournament.active_match_ids:
            match = self._matches.get(match_id)
            if match is not None and match.thread_id == thread_id and not match.finished:
                return match
        return None

    def _matches_for_user(self, tournament: TournamentState, user_id: int) -> list[TournamentMatchState]:
        matches: list[TournamentMatchState] = []
        for match_id in tournament.active_match_ids:
            match = self._matches.get(match_id)
            if match is None or match.finished:
                continue
            if user_id in {match.player1_user_id, match.player2_user_id}:
                matches.append(match)
        return matches

    def _resolve_moderation_match(
        self,
        tournament: TournamentState,
        *,
        channel: discord.abc.Messageable | discord.abc.GuildChannel | discord.Thread,
        target_user_id: int | None = None,
    ) -> tuple[TournamentMatchState | None, str | None]:
        if isinstance(channel, discord.Thread):
            match = self._match_for_thread(tournament, channel.id)
            if match is not None:
                return match, None
        if target_user_id is not None:
            matches = self._matches_for_user(tournament, target_user_id)
            if len(matches) == 1:
                return matches[0], None
            if len(matches) > 1:
                return None, "That player appears in more than one unresolved match. Run the command inside the correct match thread."
        active_matches = [self._matches[mid] for mid in tournament.active_match_ids if mid in self._matches and not self._matches[mid].finished]
        if len(active_matches) == 1:
            return active_matches[0], None
        return None, "I could not uniquely identify the match. Mention a player in that match or run the command inside the match thread."

    async def _pause_tournament_runtime(self, tournament: TournamentState) -> None:
        tournament.status = "paused"
        await self._set_tournament_status(tournament.tournament_id, "paused")
        for match_id in list(tournament.active_match_ids):
            task = self._match_tasks.pop(match_id, None)
            if task and not task.done():
                task.cancel()
            match = self._matches.get(match_id)
            if match is not None:
                await self._edit_match_message(match, disabled=True, notice="⏸️ Paused by a moderator.")
                await self._save_match_runtime(match)
        await self._save_tournament_runtime(tournament)

    async def _resume_tournament_runtime(self, tournament: TournamentState) -> None:
        tournament.status = "active"
        await self._set_tournament_status(tournament.tournament_id, "active")
        await self._save_tournament_runtime(tournament)
        for match_id in list(tournament.active_match_ids):
            match = self._matches.get(match_id)
            if match is None:
                continue
            await self._restore_match_after_ready(match, paused=False)

    async def _admin_finish_match(
        self,
        match: TournamentMatchState,
        winner_user_id: int,
        *,
        reason: str,
        penalize_user_id: int | None = None,
    ) -> None:
        if winner_user_id not in {match.player1_user_id, match.player2_user_id}:
            raise ValueError("winner_user_id must be one of the match players")
        task = self._match_tasks.pop(match.match_id, None)
        if task and not task.done():
            task.cancel()
        captured_guesses = {uid: guess for uid, (guess, _ts) in match.guesses.items()}
        match.history.append(
            TournamentMatchResult(
                question_number=match.current_question,
                event_id=match.event_id,
                prompt=f"[Admin decision] {reason}",
                correct_year=match.correct_year,
                guesses=captured_guesses,
                winner_user_id=winner_user_id,
                winner_guess=None,
                winner_diff=None,
            )
        )
        match.guesses.clear()
        match.scores[winner_user_id] = max(match.scores.get(winner_user_id, 0), match.wins_needed)
        match.winner_user_id = winner_user_id
        match.finished = True
        await self._update_match_result(match)
        if penalize_user_id is not None:
            await self._record_no_show(match.guild_id, penalize_user_id, inactivity_dqs=1)
        thread = await self._resolve_channel(match.thread_id)
        if isinstance(thread, discord.Thread):
            await self._send_managed_message(thread, f"🛠️ Moderator ruling: <@{winner_user_id}> advances. Reason: {reason}")
        await self._handle_finished_match(match)

    async def _admin_remake_match(self, match: TournamentMatchState, *, reason: str) -> None:
        task = self._match_tasks.pop(match.match_id, None)
        if task and not task.done():
            task.cancel()
        thread = await self._ensure_match_thread(match, create_if_missing=True)
        evt = self._pick_event()
        if evt is None:
            raise RuntimeError("No GuessYear events are available for tournament play.")
        match.event_id = str(evt["id"])
        match.correct_year = int(evt["year"])
        match.prompt = str(evt["prompt"])
        match.current_question = 1
        match.round_started_at = 0
        match.round_ends_at = 0
        match.scores = {match.player1_user_id: 0, match.player2_user_id: 0}
        match.guesses.clear()
        match.history.clear()
        match.finished = False
        match.winner_user_id = None
        match.status_message_id = None
        await self._update_match_result(match)
        await self._save_match_runtime(match)
        if isinstance(thread, discord.Thread):
            await self._send_managed_message(thread, f"🔁 Match remade by a moderator. Reason: {reason}")
        await self._post_match_question(match)

    async def _admin_finish_tournament(self, tournament: TournamentState, *, winner_user_id: int, reason: str) -> None:
        tournament.status = "finished"
        tournament.winner_user_id = winner_user_id
        for match_id in list(tournament.active_match_ids):
            task = self._match_tasks.pop(match_id, None)
            if task and not task.done():
                task.cancel()
            await self._delete_match_runtime(match_id)
            self._matches.pop(match_id, None)
        tournament.active_match_ids.clear()
        await self._set_tournament_status(tournament.tournament_id, "finished", winner_user_id=winner_user_id)
        await self._season_add_points(tournament.guild_id, winner_user_id, points=8, tournament_wins=1)
        host_channel = await self._resolve_channel(tournament.channel_id)
        if isinstance(host_channel, discord.TextChannel):
            await self._send_managed_message(host_channel, f"🏆 Moderator ended **{tournament.title}** and awarded the title to <@{winner_user_id}>. Reason: {reason}")
            await self._refresh_bracket_message(host_channel, tournament)
            await self._apply_champion_role(host_channel.guild, winner_user_id)
        await self._delete_tournament_runtime(tournament)


    def _build_bracket_embed(self, tournament: TournamentState, guild: discord.Guild | None) -> discord.Embed:
        lines: list[str] = []
        for round_number in sorted(tournament.round_pairings):
            lines.append(f"**Round {round_number}**")
            pairings = tournament.round_pairings.get(round_number, [])
            winners = tournament.round_winners.get(round_number, {})
            for idx, (player1, player2) in enumerate(pairings, start=1):
                if player2 is None:
                    lines.append(f"Match {idx}: {self._user_ref(guild, player1)} gets a bye")
                    continue
                winner_user_id = winners.get(idx)
                status = f" — winner: {self._user_ref(guild, winner_user_id)}" if winner_user_id else " — in progress"
                lines.append(
                    f"Match {idx}: {self._user_ref(guild, player1)} vs {self._user_ref(guild, player2)}{status}"
                )
            lines.append("")
        description = "\n".join(lines).strip() or "Bracket will appear once matches are seeded."
        embed = discord.Embed(
            title=f"{tournament.title} • Bracket Board",
            description=description,
            color=discord.Color.gold(),
        )
        if tournament.winner_user_id:
            embed.add_field(name="Champion", value=self._user_ref(guild, tournament.winner_user_id), inline=False)
        else:
            embed.add_field(name="Status", value=f"{tournament.status.title()} • Round {tournament.round_number}", inline=False)
        return embed

    async def _refresh_bracket_message(self, host_channel: discord.TextChannel, tournament: TournamentState) -> None:
        guild = host_channel.guild
        embed = self._build_bracket_embed(tournament, guild)
        if tournament.bracket_message_id is None:
            msg = await host_channel.send(embed=embed)
            tournament.bracket_message_id = msg.id
            await self._save_tournament_runtime(tournament)
            return
        try:
            msg = await host_channel.fetch_message(tournament.bracket_message_id)
            await msg.edit(embed=embed)
        except Exception:
            msg = await host_channel.send(embed=embed)
            tournament.bracket_message_id = msg.id
        await self._save_tournament_runtime(tournament)

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

    def _signup_view_for(self, state: TournamentSignupState) -> discord.ui.View:
        if state.status == "ready_check":
            return ReadyCheckView(self, state)
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
        await self._save_signup_runtime(state)
        warning_suffix = await self._join_warning_suffix(state.guild_id, interaction.user.id)
        await interaction.response.send_message("You joined the tournament." + warning_suffix, ephemeral=True)
        await self._refresh_signup_message(state)

    async def _leave_signup(self, interaction: discord.Interaction, state: TournamentSignupState) -> None:
        if interaction.user.id not in state.entrants:
            await interaction.response.send_message("You are not currently in this tournament.", ephemeral=True)
            return
        state.entrants.discard(interaction.user.id)
        await self._remove_entry(state.tournament_id, interaction.user.id)
        await self._save_signup_runtime(state)
        await interaction.response.send_message("You left the tournament.", ephemeral=True)
        await self._refresh_signup_message(state)

    async def _cancel_signup(self, interaction: discord.Interaction, state: TournamentSignupState) -> None:
        self._signups.pop((state.guild_id, state.channel_id), None)
        state.status = "cancelled"
        await self._cancel_auto_signup_task(state.guild_id, state.channel_id)
        await self._cancel_ready_check_task(state.guild_id, state.channel_id)
        await self._set_tournament_status(state.tournament_id, "cancelled")
        await self._delete_signup_runtime(state)
        view = self._signup_view_for(state)
        for child in view.children:
            child.disabled = True
        embed = self._build_signup_embed(state, interaction.guild)
        embed.color = discord.Color.dark_grey()
        embed.title = f"{state.title} • Cancelled"
        await interaction.response.edit_message(embed=embed, view=view)

    async def _start_signup_tournament(self, interaction: discord.Interaction, state: TournamentSignupState) -> None:
        if state.status != "signup":
            await interaction.response.send_message("This tournament has already moved past signup.", ephemeral=True)
            return
        entrants = list(state.entrants)
        if len(entrants) < 2:
            await interaction.response.send_message("You need at least 2 entrants to begin ready check.", ephemeral=True)
            return
        await self._cancel_auto_signup_task(state.guild_id, state.channel_id)
        await self._begin_ready_check(interaction.channel, state)
        await interaction.response.send_message("Ready check started. Entrants were pinged in DMs where possible.", ephemeral=True)

    def _seed_pairings_with_byes(self, players: list[int]) -> list[tuple[int, int | None]]:
        seeded = self._dedupe_players(list(players))
        bracket_slots = 1
        while bracket_slots < max(2, len(seeded)):
            bracket_slots *= 2
        byes_needed = max(0, bracket_slots - len(seeded))
        bye_players = seeded[:byes_needed]
        remaining = seeded[byes_needed:]
        pairings: list[tuple[int, int | None]] = [(user_id, None) for user_id in bye_players]
        while remaining:
            player1 = remaining.pop(0)
            player2 = remaining.pop(0) if remaining else None
            if player2 is not None and player1 == player2:
                pairings.append((player1, None))
            else:
                pairings.append((player1, player2))
        return pairings

    async def _launch_next_pending_match(self, host_channel: discord.TextChannel, tournament: TournamentState) -> TournamentMatchState | None:
        if tournament.active_match_ids or not tournament.pending_matchups:
            return None
        bracket_position, player1_user_id, player2_user_id, is_final = tournament.pending_matchups.pop(0)
        if player1_user_id == player2_user_id:
            tournament.next_round_players.append(player1_user_id)
            tournament.round_winners.setdefault(tournament.round_number, {})[bracket_position] = player1_user_id
            await self._save_tournament_runtime(tournament)
            await self._refresh_bracket_message(host_channel, tournament)
            if not tournament.pending_matchups:
                await self._advance_or_finish(host_channel, tournament)
            return None
        if is_final:
            guild = self.bot.get_guild(tournament.guild_id)
            await self._award_finalist_points(tournament, [player1_user_id, player2_user_id])
            await self._send_managed_message(
                host_channel,
                f"🔥 **Final is live** — {self._user_ref(guild, player1_user_id)} vs {self._user_ref(guild, player2_user_id)}"
            )
        match = await self._launch_match(
            host_channel,
            tournament,
            tournament.round_number,
            bracket_position,
            player1_user_id,
            player2_user_id,
            is_final=is_final,
        )
        tournament.active_match_ids.add(match.match_id)
        await self._save_match_runtime(match)
        await self._save_tournament_runtime(tournament)
        await self._refresh_bracket_message(host_channel, tournament)
        return match

    async def _start_round(self, host_channel: discord.abc.Messageable, tournament: TournamentState, players: list[int]) -> None:
        round_number = tournament.round_number
        tournament.next_round_players = []
        tournament.active_match_ids.clear()
        tournament.pending_matchups = []

        normalized_players = self._dedupe_players(players)
        pairings = self._seed_pairings_with_byes(normalized_players)

        tournament.round_pairings[round_number] = pairings
        tournament.round_winners.setdefault(round_number, {})

        is_final_round = len([pair for pair in pairings if pair[1] is not None]) == 1 and len(pairings) == 1

        for idx, (player1, player2) in enumerate(pairings, start=1):
            if player2 is None:
                tournament.next_round_players.append(player1)
                tournament.round_winners[round_number][idx] = player1
                continue
            tournament.pending_matchups.append((idx, player1, player2, is_final_round))

        await self._save_tournament_runtime(tournament)

        if isinstance(host_channel, discord.TextChannel):
            await self._refresh_bracket_message(host_channel, tournament)
            await self._launch_next_pending_match(host_channel, tournament)

        if not tournament.active_match_ids and not tournament.pending_matchups:
            await self._advance_or_finish(host_channel, tournament)

    async def _launch_match(
        self,
        host_channel: discord.abc.Messageable,
        tournament: TournamentState,
        round_number: int,
        bracket_position: int,
        player1_user_id: int,
        player2_user_id: int,
        *,
        is_final: bool = False,
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
        if player1_user_id == player2_user_id:
            raise RuntimeError("Refusing to create a duel tournament match where both players are the same user.")

        p1 = host_channel.guild.get_member(player1_user_id)
        p2 = host_channel.guild.get_member(player2_user_id)
        seed_message = await self._send_managed_message(host_channel,
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
            is_final=is_final,
            scores={player1_user_id: 0, player2_user_id: 0},
        )
        self._matches[match.match_id] = match
        await self._update_match_thread(match.match_id, thread.id)
        await self._save_match_runtime(match)
        await self._post_match_question(match)
        return match

    async def _disable_previous_match_prompt(self, match: TournamentMatchState) -> None:
        thread = await self._ensure_match_thread(match, create_if_missing=True)
        if not isinstance(thread, discord.Thread) or match.status_message_id is None:
            return
        try:
            previous = await thread.fetch_message(match.status_message_id)
        except Exception:
            return
        guild = self.bot.get_guild(match.guild_id)
        if guild is None:
            return
        disabled_view = self._build_match_view(match, disabled=True)
        disabled_embed = self._build_match_embed(match, guild)
        disabled_embed.set_footer(text="Question closed. Wait for the next prompt.")
        try:
            await previous.edit(embed=disabled_embed, view=disabled_view)
        except Exception:
            pass

    async def _post_match_question(self, match: TournamentMatchState) -> None:
        thread = await self._ensure_match_thread(match, create_if_missing=True)
        guild = self.bot.get_guild(match.guild_id)
        if not isinstance(thread, discord.Thread) or guild is None:
            return
        match.round_started_at = int(time.time())
        match.round_ends_at = match.round_started_at + 60
        match.guesses.clear()

        await self._disable_previous_match_prompt(match)
        prompt_message = await self._send_managed_message(
            thread,
            embed=self._build_match_embed(match, guild),
            view=self._build_match_view(match, disabled=False),
        )
        match.status_message_id = prompt_message.id
        await self._save_match_runtime(match)

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
        tournament = self._tournaments.get(match.tournament_id)
        if tournament is None or tournament.status != "active":
            await interaction.response.send_message("This match is currently paused or unavailable.", ephemeral=True)
            return
        if match.finished:
            await interaction.response.send_message("This match is already finished.", ephemeral=True)
            return
        if interaction.user.id in match.guesses:
            await interaction.response.send_message("You already locked a guess for this question.", ephemeral=True)
            return
        await interaction.response.send_modal(TournamentGuessModal(self, match))

    async def _submit_hidden_guess(self, interaction: discord.Interaction, match: TournamentMatchState, raw_guess: str) -> None:
        tournament = self._tournaments.get(match.tournament_id)
        if tournament is None or tournament.status != "active":
            await interaction.response.send_message("This match is currently paused or unavailable.", ephemeral=True)
            return
        parsed = YEAR_RE.match(raw_guess)
        if not parsed:
            await interaction.response.send_message("Enter a valid year like 1066 or 1914.", ephemeral=True)
            return
        guess_year = int(parsed.group(1))
        match.guesses[interaction.user.id] = (guess_year, int(time.time()))
        await self._save_match_runtime(match)
        await interaction.response.send_message(f"Locked in: **{guess_year}**", ephemeral=True)
        if len(match.guesses) >= 2:
            task = self._match_tasks.get(match.match_id)
            if task and not task.done():
                task.cancel()
            await self._resolve_match_question(match)

    async def _resolve_match_question(self, match: TournamentMatchState) -> None:
        if match.finished:
            return
        thread = await self._resolve_channel(match.thread_id)
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
        await self._save_match_runtime(match)

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

        await self._send_managed_message(thread, "\n".join(lines))

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
        host_channel = await self._resolve_channel(tournament.channel_id)
        if not isinstance(host_channel, discord.TextChannel):
            return
        thread = await self._resolve_channel(match.thread_id)
        score1 = match.scores.get(match.player1_user_id, 0)
        score2 = match.scores.get(match.player2_user_id, 0)
        winner_mention = f"<@{match.winner_user_id}>" if match.winner_user_id else "No winner"
        if match.winner_user_id is not None:
            await self._season_add_points(tournament.guild_id, match.winner_user_id, points=3, match_wins=1)
            tournament.next_round_players.append(match.winner_user_id)
            tournament.round_winners.setdefault(match.round_number, {})[match.bracket_position] = match.winner_user_id

        submission_counts = self._match_submission_counts(match)
        flagged_user_ids = [user_id for user_id, submitted in submission_counts.items() if submitted <= 0]
        for user_id in flagged_user_ids:
            await self._record_no_show(tournament.guild_id, user_id, match_no_shows=1)

        await self._send_managed_message(
            host_channel,
            f"🏁 **Round {match.round_number}, Match {match.bracket_position} finished** — "
            f"winner: {winner_mention} (`{score1}-{score2}`)"
            + (f" • Thread: {thread.mention}" if isinstance(thread, discord.Thread) else "")
            + (
                f" • no-show flagged: {', '.join(f'<@{user_id}>' for user_id in flagged_user_ids)}"
                if flagged_user_ids else ""
            )
        )
        tournament.active_match_ids.discard(match.match_id)
        task = self._match_tasks.pop(match.match_id, None)
        if task and not task.done():
            task.cancel()
        await self._delete_match_runtime(match)
        await self._refresh_bracket_message(host_channel, tournament)
        await self._save_tournament_runtime(tournament)
        if not tournament.active_match_ids:
            if tournament.pending_matchups:
                await self._launch_next_pending_match(host_channel, tournament)
            else:
                await self._advance_or_finish(host_channel, tournament)

    async def _advance_or_finish(self, host_channel: discord.TextChannel, tournament: TournamentState) -> None:
        players = self._dedupe_players(list(tournament.next_round_players))
        tournament.next_round_players = players
        tournament.pending_matchups = []
        if len(players) <= 1:
            tournament.status = "finished"
            tournament.winner_user_id = players[0] if players else None
            await self._set_tournament_status(tournament.tournament_id, "finished", winner_user_id=tournament.winner_user_id)
            if tournament.winner_user_id is not None:
                await self._season_add_points(tournament.guild_id, tournament.winner_user_id, points=8, tournament_wins=1)
                await self._send_managed_message(
                    host_channel,
                    f"🏆 **{tournament.title} champion:** <@{tournament.winner_user_id}>"
                )
                await self._apply_champion_role(host_channel.guild, tournament.winner_user_id)
            else:
                await self._send_managed_message(host_channel, f"{tournament.title} ended without a winner.")
            await self._refresh_bracket_message(host_channel, tournament)
            await self._delete_tournament_runtime(tournament)
            return
        tournament.round_number += 1
        tournament.next_round_players = []
        await self._save_tournament_runtime(tournament)
        await self._start_round(host_channel, tournament, players)

    async def _all_time_profile(self, guild_id: int, user_id: int) -> dict[str, int]:
        cur = await self.bot.db.conn.execute(
            """
            SELECT
              COALESCE((SELECT COUNT(DISTINCT e.tournament_id)
                        FROM guessyear_tournament_entries e
                        JOIN guessyear_tournaments t ON t.tournament_id=e.tournament_id
                        WHERE t.guild_id=? AND e.user_id=? AND t.status IN ('active', 'finished')), 0),
              COALESCE((SELECT COUNT(*)
                        FROM guessyear_tournament_matches m
                        JOIN guessyear_tournaments t ON t.tournament_id=m.tournament_id
                        WHERE t.guild_id=? AND m.winner_user_id=?), 0),
              COALESCE((SELECT COUNT(*)
                        FROM guessyear_tournaments t
                        WHERE t.guild_id=? AND t.winner_user_id=? AND t.status='finished'), 0),
              COALESCE((SELECT COUNT(*)
                        FROM guessyear_tournament_matches m
                        JOIN guessyear_tournaments t ON t.tournament_id=m.tournament_id
                        JOIN (
                            SELECT tournament_id, MAX(round_number) AS final_round
                            FROM guessyear_tournament_matches
                            GROUP BY tournament_id
                        ) finals ON finals.tournament_id=m.tournament_id AND finals.final_round=m.round_number
                        WHERE t.guild_id=? AND t.status='finished' AND (m.player1_user_id=? OR m.player2_user_id=?)), 0)
            """,
            (str(guild_id), str(user_id), str(guild_id), str(user_id), str(guild_id), str(user_id), str(guild_id), str(user_id), str(user_id)),
        )
        row = await cur.fetchone()
        return {
            'cups_entered': int(row[0] or 0),
            'match_wins': int(row[1] or 0),
            'titles': int(row[2] or 0),
            'finals_reached': int(row[3] or 0),
        }


    async def _all_time_leaderboard(self, guild_id: int, limit: int = 10) -> list[tuple[int, int, int, int, int]]:
        cur = await self.bot.db.conn.execute(
            """
            WITH participants AS (
                SELECT DISTINCT CAST(e.user_id AS INTEGER) AS user_id
                FROM guessyear_tournament_entries e
                JOIN guessyear_tournaments t ON t.tournament_id=e.tournament_id
                WHERE t.guild_id=? AND t.status IN ('active', 'finished')
                UNION
                SELECT DISTINCT CAST(m.winner_user_id AS INTEGER) AS user_id
                FROM guessyear_tournament_matches m
                JOIN guessyear_tournaments t ON t.tournament_id=m.tournament_id
                WHERE t.guild_id=? AND m.winner_user_id IS NOT NULL
            ),
            entered AS (
                SELECT CAST(e.user_id AS INTEGER) AS user_id, COUNT(DISTINCT e.tournament_id) AS cups_entered
                FROM guessyear_tournament_entries e
                JOIN guessyear_tournaments t ON t.tournament_id=e.tournament_id
                WHERE t.guild_id=? AND t.status IN ('active', 'finished')
                GROUP BY e.user_id
            ),
            wins AS (
                SELECT CAST(m.winner_user_id AS INTEGER) AS user_id, COUNT(*) AS match_wins
                FROM guessyear_tournament_matches m
                JOIN guessyear_tournaments t ON t.tournament_id=m.tournament_id
                WHERE t.guild_id=? AND m.winner_user_id IS NOT NULL
                GROUP BY m.winner_user_id
            ),
            titles AS (
                SELECT CAST(t.winner_user_id AS INTEGER) AS user_id, COUNT(*) AS titles
                FROM guessyear_tournaments t
                WHERE t.guild_id=? AND t.status='finished' AND t.winner_user_id IS NOT NULL
                GROUP BY t.winner_user_id
            ),
            finals AS (
                SELECT CAST(players.user_id AS INTEGER) AS user_id, COUNT(*) AS finals_reached
                FROM (
                    SELECT m.tournament_id, m.round_number, m.player1_user_id AS user_id FROM guessyear_tournament_matches m
                    UNION ALL
                    SELECT m.tournament_id, m.round_number, m.player2_user_id AS user_id FROM guessyear_tournament_matches m
                ) players
                JOIN guessyear_tournaments t ON t.tournament_id=players.tournament_id
                JOIN (
                    SELECT tournament_id, MAX(round_number) AS final_round
                    FROM guessyear_tournament_matches
                    GROUP BY tournament_id
                ) final_rounds ON final_rounds.tournament_id=players.tournament_id AND final_rounds.final_round=players.round_number
                WHERE t.guild_id=? AND t.status='finished' AND players.user_id IS NOT NULL
                GROUP BY players.user_id
            )
            SELECT
                participants.user_id,
                COALESCE(entered.cups_entered, 0) AS cups_entered,
                COALESCE(wins.match_wins, 0) AS match_wins,
                COALESCE(titles.titles, 0) AS titles,
                COALESCE(finals.finals_reached, 0) AS finals_reached
            FROM participants
            LEFT JOIN entered ON entered.user_id=participants.user_id
            LEFT JOIN wins ON wins.user_id=participants.user_id
            LEFT JOIN titles ON titles.user_id=participants.user_id
            LEFT JOIN finals ON finals.user_id=participants.user_id
            ORDER BY titles DESC, match_wins DESC, finals_reached DESC, cups_entered DESC, participants.user_id ASC
            LIMIT ?
            """,
            (
                str(guild_id),
                str(guild_id),
                str(guild_id),
                str(guild_id),
                str(guild_id),
                str(guild_id),
                int(limit),
            ),
        )
        rows = await cur.fetchall()
        return [
            (int(row[0]), int(row[1] or 0), int(row[2] or 0), int(row[3] or 0), int(row[4] or 0))
            for row in rows
        ]

    async def _recent_tournament_history(self, guild_id: int, limit: int = 8) -> list[tuple[int, str, int, int | None, int, int]]:
        cur = await self.bot.db.conn.execute(
            """
            SELECT t.tournament_id, t.title, t.ended_at, t.winner_user_id, t.bracket_size,
                   COALESCE((SELECT COUNT(*) FROM guessyear_tournament_entries e WHERE e.tournament_id=t.tournament_id), 0) AS entrants
            FROM guessyear_tournaments t
            WHERE t.guild_id=? AND t.status='finished'
            ORDER BY t.ended_at DESC
            LIMIT ?
            """,
            (str(guild_id), int(limit)),
        )
        rows = await cur.fetchall()
        parsed: list[tuple[int, str, int, int | None, int, int]] = []
        for row in rows:
            parsed.append((
                int(row[0]), str(row[1]), int(row[2] or 0), int(row[3]) if row[3] is not None else None, int(row[4] or 0), int(row[5] or 0)
            ))
        return parsed

    def _parse_weekday(self, raw: str) -> int | None:
        lookup = {
            "mon": 0, "monday": 0,
            "tue": 1, "tues": 1, "tuesday": 1,
            "wed": 2, "wednesday": 2,
            "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
            "fri": 4, "friday": 4,
            "sat": 5, "saturday": 5,
            "sun": 6, "sunday": 6,
        }
        return lookup.get(raw.strip().lower())

    def _format_weekday(self, weekday: int) -> str:
        names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        if 0 <= weekday < len(names):
            return names[weekday]
        return str(weekday)

    def _parse_utc_time(self, raw: str) -> tuple[int, int] | None:
        m = re.match(r"^([01]?\d|2[0-3]):([0-5]\d)$", raw.strip())
        if not m:
            return None
        return int(m.group(1)), int(m.group(2))

    @commands.group(name="dueltourney", invoke_without_command=True)
    async def dueltourney(self, ctx: commands.Context) -> None:
        await ctx.send(
            "Use `!dueltourney open`, `!dueltourney join`, `!dueltourney leave`, `!dueltourney start`, "
            "`!dueltourney ready`, `!dueltourney drop`, `!dueltourney noshow`, `!dueltourney pause`, `!dueltourney resume`, "
            "`!dueltourney dq`, `!dueltourney advance`, `!dueltourney remake`, `!dueltourney status`, or `!dueltourney cancel`."
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
        title = "Friday Mini Duel Cup"
        await self._open_signup_internal(
            ctx.channel,
            created_by_user_id=ctx.author.id,
            title=title,
            bracket_size=size,
            best_of=best_of,
            auto_join_user_id=ctx.author.id,
            signup_window_minutes=None,
        )

    @dueltourney.command(name="join")
    async def dueltourney_join(self, ctx: commands.Context) -> None:
        if not ctx.guild:
            return
        state = self._signups.get((ctx.guild.id, ctx.channel.id))
        if not state or state.status != "signup":
            await ctx.send("There is no open tournament signup in this channel.", delete_after=10)
            return
        if len(state.entrants) >= state.bracket_size and ctx.author.id not in state.entrants:
            await ctx.send("The bracket is already full.", delete_after=10)
            return
        state.entrants.add(ctx.author.id)
        await self._upsert_entry(state.tournament_id, ctx.author.id)
        await self._save_signup_runtime(state)
        await self._refresh_signup_message(state)
        await ctx.message.add_reaction("✅")
        warning_suffix = await self._join_warning_suffix(ctx.guild.id, ctx.author.id)
        if warning_suffix:
            await ctx.send("You joined the tournament." + warning_suffix, delete_after=15)

    @dueltourney.command(name="leave")
    async def dueltourney_leave(self, ctx: commands.Context) -> None:
        if not ctx.guild:
            return
        state = self._signups.get((ctx.guild.id, ctx.channel.id))
        if not state:
            await ctx.send("There is no open tournament signup in this channel.", delete_after=10)
            return
        if state.status == "ready_check":
            await ctx.send("Ready check is active. Use `!dueltourney drop` if you need to withdraw.", delete_after=10)
            return
        if ctx.author.id not in state.entrants:
            await ctx.send("You are not signed up for this tournament.", delete_after=10)
            return
        state.entrants.discard(ctx.author.id)
        await self._remove_entry(state.tournament_id, ctx.author.id)
        await self._save_signup_runtime(state)
        await self._refresh_signup_message(state)
        await ctx.message.add_reaction("👋")

    def _find_ready_check_for_user(self, user_id: int, guild_id: int | None = None, channel_id: int | None = None) -> TournamentSignupState | None:
        matches: list[TournamentSignupState] = []
        for state in self._signups.values():
            if state.status != "ready_check":
                continue
            if user_id not in state.entrants:
                continue
            if guild_id is not None and state.guild_id != guild_id:
                continue
            if channel_id is not None and state.channel_id != channel_id:
                continue
            matches.append(state)
        if len(matches) == 1:
            return matches[0]
        return None

    @dueltourney.command(name="ready")
    async def dueltourney_ready(self, ctx: commands.Context) -> None:
        state: TournamentSignupState | None
        if ctx.guild and isinstance(ctx.channel, discord.TextChannel):
            state = self._find_ready_check_for_user(ctx.author.id, ctx.guild.id, ctx.channel.id)
        else:
            state = self._find_ready_check_for_user(ctx.author.id)
            if state is None:
                count = sum(1 for s in self._signups.values() if s.status == "ready_check" and ctx.author.id in s.entrants)
                if count > 1:
                    await ctx.send("You are in more than one ready check. Use `!dueltourney ready` in the correct server channel.")
                    return
        if state is None:
            await ctx.send("You are not currently in an active ready check.")
            return
        _ok, message = await self._mark_ready(state, ctx.author.id)
        await ctx.send(message)

    @dueltourney.command(name="drop")
    async def dueltourney_drop(self, ctx: commands.Context) -> None:
        state: TournamentSignupState | None
        if ctx.guild and isinstance(ctx.channel, discord.TextChannel):
            state = self._find_ready_check_for_user(ctx.author.id, ctx.guild.id, ctx.channel.id)
        else:
            state = self._find_ready_check_for_user(ctx.author.id)
            if state is None:
                count = sum(1 for s in self._signups.values() if s.status == "ready_check" and ctx.author.id in s.entrants)
                if count > 1:
                    await ctx.send("You are in more than one ready check. Use `!dueltourney drop` in the correct server channel.")
                    return
        if state is None:
            await ctx.send("You are not currently in an active ready check.")
            return
        _ok, message = await self._drop_ready_participant(state, ctx.author.id)
        await ctx.send(message)

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
            await ctx.send("You need at least 2 entrants to begin ready check.", delete_after=10)
            return
        await self._cancel_auto_signup_task(ctx.guild.id, ctx.channel.id)
        await self._begin_ready_check(ctx.channel, state)
        try:
            await ctx.message.add_reaction("📨")
        except Exception:
            pass

    @dueltourney.command(name="schedule")
    async def dueltourney_schedule(
        self,
        ctx: commands.Context,
        weekday_or_status: str | None = None,
        time_utc: str | None = None,
        signup_minutes: int = 10,
        size: int = 8,
        best_of: int = 3,
    ) -> None:
        if not ctx.guild or not isinstance(ctx.channel, discord.TextChannel):
            return
        if not self._can_manage(ctx.author):
            await ctx.send("Only moderators can schedule the Friday duel tournament.", delete_after=10)
            return
        if weekday_or_status is None or weekday_or_status.lower() == "help":
            await ctx.send(
                "Use `!dueltourney schedule fri 19:00 [signup_minutes] [cap] [best_of]` or `!dueltourney schedule status`. Times are UTC.",
                delete_after=20,
            )
            return
        if weekday_or_status.lower() == "status":
            schedule = self._schedules.get((ctx.guild.id, ctx.channel.id))
            if not schedule:
                await ctx.send("No recurring duel tournament is scheduled for this channel.", delete_after=10)
                return
            embed = discord.Embed(
                title="Scheduled Duel Tournament",
                description=(
                    f"**When:** {self._format_weekday(schedule.weekday)} {schedule.hour_utc:02d}:{schedule.minute_utc:02d} UTC\n"
                    f"**Signup window:** {schedule.signup_window_minutes} minute(s)\n"
                    f"**Signup cap:** {schedule.bracket_size}\n"
                    f"**Best of:** {schedule.best_of}"
                ),
                color=discord.Color.gold(),
            )
            if schedule.last_fire_key:
                embed.set_footer(text=f"Last fired key: {schedule.last_fire_key}")
            await ctx.send(embed=embed)
            return
        weekday = self._parse_weekday(weekday_or_status)
        parsed_time = self._parse_utc_time(time_utc or "")
        if weekday is None or parsed_time is None:
            await ctx.send("Example: `!dueltourney schedule fri 19:00 10 8 3` (UTC).", delete_after=15)
            return
        hour_utc, minute_utc = parsed_time
        if signup_minutes < 1 or signup_minutes > 180:
            await ctx.send("signup_minutes must be between 1 and 180.", delete_after=10)
            return
        if size < 2 or size > 16:
            await ctx.send("Signup cap must be between 2 and 16.", delete_after=10)
            return
        if best_of not in {1, 3, 5}:
            await ctx.send("best_of must be 1, 3, or 5.", delete_after=10)
            return
        schedule = await self._upsert_schedule(
            ctx.guild.id,
            ctx.channel.id,
            ctx.author.id,
            "Friday Mini Duel Cup",
            weekday,
            hour_utc,
            minute_utc,
            signup_minutes,
            size,
            best_of,
        )
        await ctx.send(
            f"✅ Scheduled **{schedule.title}** for **{self._format_weekday(schedule.weekday)} {schedule.hour_utc:02d}:{schedule.minute_utc:02d} UTC** "
            f"with a **{schedule.signup_window_minutes}-minute** signup window."
        )

    @dueltourney.command(name="unschedule")
    async def dueltourney_unschedule(self, ctx: commands.Context) -> None:
        if not ctx.guild:
            return
        if not self._can_manage(ctx.author):
            await ctx.send("Only moderators can remove the duel tournament schedule.", delete_after=10)
            return
        schedule = self._schedules.get((ctx.guild.id, ctx.channel.id))
        if not schedule:
            await ctx.send("No recurring duel tournament is scheduled for this channel.", delete_after=10)
            return
        await self._delete_schedule(ctx.guild.id, ctx.channel.id)
        await ctx.send("🗑️ Removed the recurring duel tournament schedule for this channel.")

    @dueltourney.command(name="profile")
    async def dueltourney_profile(self, ctx: commands.Context, member: discord.Member | None = None) -> None:
        if not ctx.guild:
            return
        target = member or ctx.author
        stats = await self._all_time_profile(ctx.guild.id, target.id)
        no_show_stats = await self._get_no_show_stats(ctx.guild.id, target.id)
        reliability = self._reliability_percent(
            cups_entered=stats['cups_entered'],
            total_flags=int(no_show_stats['total_flags'] or 0),
        )
        season, standings = await self._season_standings(ctx.guild.id, limit=100)
        season_row = next((row for row in standings if row[0] == target.id), None)
        embed = discord.Embed(
            title=f"Duel Tournament Profile • {target.display_name}",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Cups entered", value=str(stats['cups_entered']), inline=True)
        embed.add_field(name="Match wins", value=str(stats['match_wins']), inline=True)
        embed.add_field(name="Titles", value=str(stats['titles']), inline=True)
        embed.add_field(name="Finals reached", value=str(stats['finals_reached']), inline=True)
        embed.add_field(name="Reliability", value=f"{reliability}%", inline=True)
        embed.add_field(name="No-show flags", value=str(no_show_stats['total_flags']), inline=True)
        embed.add_field(
            name="No-show detail",
            value=(
                f"Ready-check misses: {no_show_stats['readycheck_missed']}\n"
                f"Match no-shows: {no_show_stats['match_no_shows']}\n"
                f"Inactivity DQs: {no_show_stats['inactivity_dqs']}"
            ),
            inline=False,
        )
        if season_row:
            embed.add_field(name=f"{season[1]} points", value=str(season_row[1]), inline=True)
            embed.add_field(name="Season match wins", value=str(season_row[3]), inline=True)
        else:
            embed.add_field(name=f"{season[1]} points", value="0", inline=True)
        await ctx.send(embed=embed)

    @dueltourney.command(name="stats")
    async def dueltourney_stats(self, ctx: commands.Context, member: discord.Member | None = None) -> None:
        await self.dueltourney_profile(ctx, member)


    @dueltourney.command(name="noshow")
    async def dueltourney_noshow(self, ctx: commands.Context, member: discord.Member | None = None) -> None:
        if not ctx.guild:
            return
        target = member or ctx.author
        stats = await self._all_time_profile(ctx.guild.id, target.id)
        no_show_stats = await self._get_no_show_stats(ctx.guild.id, target.id)
        reliability = self._reliability_percent(
            cups_entered=stats['cups_entered'],
            total_flags=int(no_show_stats['total_flags'] or 0),
        )
        embed = discord.Embed(
            title=f"No-show record • {target.display_name}",
            color=discord.Color.orange() if int(no_show_stats['total_flags'] or 0) else discord.Color.green(),
        )
        embed.add_field(name="Reliability", value=f"{reliability}%", inline=True)
        embed.add_field(name="Total flags", value=str(no_show_stats['total_flags']), inline=True)
        embed.add_field(name="Cups entered", value=str(stats['cups_entered']), inline=True)
        embed.add_field(name="Missed ready-checks", value=str(no_show_stats['readycheck_missed']), inline=True)
        embed.add_field(name="Match no-shows", value=str(no_show_stats['match_no_shows']), inline=True)
        embed.add_field(name="Inactivity DQs", value=str(no_show_stats['inactivity_dqs']), inline=True)
        last_no_show_at = no_show_stats['last_no_show_at']
        embed.add_field(
            name="Last flagged",
            value=(f"<t:{int(last_no_show_at)}:R>" if last_no_show_at else "No no-show events recorded."),
            inline=False,
        )
        await ctx.send(embed=embed)

    @dueltourney.command(name="history")
    async def dueltourney_history(self, ctx: commands.Context) -> None:
        if not ctx.guild:
            return
        history = await self._recent_tournament_history(ctx.guild.id)
        if not history:
            await ctx.send("No finished duel tournaments yet.", delete_after=10)
            return
        lines = []
        for _tid, title, ended_at, winner_user_id, bracket_size, entrants in history:
            winner = f"<@{winner_user_id}>" if winner_user_id else "No winner"
            timestamp = f"<t:{ended_at}:R>" if ended_at else "Unknown time"
            lines.append(f"**{title}** — winner: {winner} • entrants: {entrants}/{bracket_size} • ended {timestamp}")
        embed = discord.Embed(title="Recent Duel Tournament History", description="\n".join(lines), color=discord.Color.gold())
        await ctx.send(embed=embed)

    @dueltourney.command(name="season")
    async def dueltourney_season(self, ctx: commands.Context) -> None:
        if not ctx.guild:
            return
        season, standings = await self._season_standings(ctx.guild.id)
        if not standings:
            await ctx.send(f"No points have been earned in **{season[1]}** yet.")
            return
        lines = []
        for idx, (user_id, points, entered, match_wins, titles, finals) in enumerate(standings[:10], start=1):
            lines.append(
                f"**{idx}.** <@{user_id}> — **{points}** pts • cups {entered} • wins {match_wins} • titles {titles} • finals {finals}"
            )
        embed = discord.Embed(
            title=f"Friday Duel Season • {season[1]}",
            description="\n".join(lines),
            color=discord.Color.gold(),
        )
        await ctx.send(embed=embed)

    @dueltourney.command(name="leaderboard")
    async def dueltourney_leaderboard(self, ctx: commands.Context) -> None:
        if not ctx.guild:
            return
        standings = await self._all_time_leaderboard(ctx.guild.id)
        if not standings:
            await ctx.send("No all-time duel tournament data yet.")
            return
        lines = []
        for idx, (user_id, cups_entered, match_wins, titles, finals_reached) in enumerate(standings[:10], start=1):
            lines.append(
                f"**{idx}.** <@{user_id}> — **{titles}** title(s) • wins {match_wins} • finals {finals_reached} • cups {cups_entered}"
            )
        embed = discord.Embed(
            title="All-Time Friday Duel Leaderboard",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="Leaderboard is all-time. Use !dueltourney season for the current monthly race.")
        await ctx.send(embed=embed)

    @dueltourney.group(name="role", invoke_without_command=True)
    async def dueltourney_role(self, ctx: commands.Context) -> None:
        await ctx.send("Use `!dueltourney role status`, `!dueltourney role champion @Role`, `!dueltourney role season @Role`, `!dueltourney role ping @Role`, or `!dueltourney role clear champion|season|ping`.")

    @dueltourney_role.command(name="status")
    async def dueltourney_role_status(self, ctx: commands.Context) -> None:
        if not ctx.guild:
            return
        config = self._get_guild_config(ctx.guild.id)
        embed = discord.Embed(title="Duel Tournament Roles", color=discord.Color.blurple())
        embed.add_field(name="Champion role", value=(f"<@&{config.champion_role_id}>" if config.champion_role_id else "Not set"), inline=False)
        embed.add_field(name="Season leader role", value=(f"<@&{config.season_role_id}>" if config.season_role_id else "Not set"), inline=False)
        embed.add_field(name="Ping role", value=(f"<@&{config.ping_role_id}>" if config.ping_role_id else "Not set"), inline=False)
        await ctx.send(embed=embed)

    @dueltourney_role.command(name="champion")
    async def dueltourney_role_champion(self, ctx: commands.Context, role: discord.Role) -> None:
        if not ctx.guild:
            return
        if not self._can_manage(ctx.author):
            await ctx.send("Only moderators can change tournament roles.", delete_after=10)
            return
        config = self._get_guild_config(ctx.guild.id)
        config.champion_role_id = role.id
        await self._save_guild_config(config)
        await ctx.send(f"✅ Champion role set to {role.mention}.")

    @dueltourney_role.command(name="season")
    async def dueltourney_role_season(self, ctx: commands.Context, role: discord.Role) -> None:
        if not ctx.guild:
            return
        if not self._can_manage(ctx.author):
            await ctx.send("Only moderators can change tournament roles.", delete_after=10)
            return
        config = self._get_guild_config(ctx.guild.id)
        config.season_role_id = role.id
        await self._save_guild_config(config)
        await self._refresh_season_leader_role(ctx.guild)
        await ctx.send(f"✅ Season leader role set to {role.mention}.")

    @dueltourney_role.command(name="ping")
    async def dueltourney_role_ping(self, ctx: commands.Context, role: discord.Role) -> None:
        if not ctx.guild:
            return
        if not self._can_manage(ctx.author):
            await ctx.send("Only moderators can change tournament roles.", delete_after=10)
            return
        config = self._get_guild_config(ctx.guild.id)
        config.ping_role_id = role.id
        await self._save_guild_config(config)
        await ctx.send(f"✅ Ping role set to {role.mention}.")

    @dueltourney_role.command(name="clear")
    async def dueltourney_role_clear(self, ctx: commands.Context, kind: str) -> None:
        if not ctx.guild:
            return
        if not self._can_manage(ctx.author):
            await ctx.send("Only moderators can change tournament roles.", delete_after=10)
            return
        config = self._get_guild_config(ctx.guild.id)
        kind = kind.lower().strip()
        if kind == 'champion':
            config.champion_role_id = None
        elif kind == 'season':
            config.season_role_id = None
        elif kind == 'ping':
            config.ping_role_id = None
        else:
            await ctx.send("Use `champion`, `season`, or `ping`.", delete_after=10)
            return
        await self._save_guild_config(config)
        await ctx.send(f"🧹 Cleared the {kind} role setting.")

    @dueltourney.group(name="reminders", invoke_without_command=True)
    async def dueltourney_reminders(self, ctx: commands.Context) -> None:
        await ctx.send("Use `!dueltourney reminders status` or `!dueltourney reminders set <before_minutes> <last_call_minutes>`.")

    @dueltourney_reminders.command(name="status")
    async def dueltourney_reminders_status(self, ctx: commands.Context) -> None:
        if not ctx.guild:
            return
        config = self._get_guild_config(ctx.guild.id)
        embed = discord.Embed(title="Duel Tournament Reminders", color=discord.Color.blurple())
        embed.add_field(name="Before tournament", value=f"{config.reminder_minutes_before} minute(s)", inline=False)
        embed.add_field(name="Last call during signup", value=f"{config.signup_last_call_minutes} minute(s)", inline=False)
        await ctx.send(embed=embed)

    @dueltourney_reminders.command(name="set")
    async def dueltourney_reminders_set(self, ctx: commands.Context, before_minutes: int, last_call_minutes: int) -> None:
        if not ctx.guild:
            return
        if not self._can_manage(ctx.author):
            await ctx.send("Only moderators can change tournament reminders.", delete_after=10)
            return
        if before_minutes < 0 or before_minutes > 1440 or last_call_minutes < 0 or last_call_minutes > 60:
            await ctx.send("Use sensible values: before 0-1440, last call 0-60.", delete_after=10)
            return
        config = self._get_guild_config(ctx.guild.id)
        config.reminder_minutes_before = before_minutes
        config.signup_last_call_minutes = last_call_minutes
        await self._save_guild_config(config)
        await ctx.send(
            f"✅ Updated reminders: {before_minutes} minute(s) before the event and {last_call_minutes} minute(s) before signup closes."
        )

    @dueltourney.command(name="pause")
    async def dueltourney_pause(self, ctx: commands.Context) -> None:
        if not ctx.guild:
            return
        if not self._can_manage(ctx.author):
            await ctx.send("Only moderators can pause a duel tournament.", delete_after=10)
            return
        tournament = self._get_live_tournament(ctx.guild.id, ctx.channel.id)
        if tournament is None:
            await ctx.send("There is no live duel tournament in this channel.", delete_after=10)
            return
        if tournament.status == "paused":
            await ctx.send("This tournament is already paused.", delete_after=10)
            return
        await self._pause_tournament_runtime(tournament)
        await ctx.send("⏸️ Paused the tournament. Active match buttons were disabled.")

    @dueltourney.command(name="resume")
    async def dueltourney_resume(self, ctx: commands.Context) -> None:
        if not ctx.guild:
            return
        if not self._can_manage(ctx.author):
            await ctx.send("Only moderators can resume a duel tournament.", delete_after=10)
            return
        tournament = self._get_live_tournament(ctx.guild.id, ctx.channel.id)
        if tournament is None:
            await ctx.send("There is no live duel tournament in this channel.", delete_after=10)
            return
        if tournament.status != "paused":
            await ctx.send("This tournament is not paused.", delete_after=10)
            return
        await self._resume_tournament_runtime(tournament)
        await ctx.send("▶️ Resumed the tournament.")

    @dueltourney.command(name="dq")
    async def dueltourney_dq(self, ctx: commands.Context, member: discord.Member) -> None:
        if not ctx.guild:
            return
        if not self._can_manage(ctx.author):
            await ctx.send("Only moderators can disqualify players.", delete_after=10)
            return
        signup = self._signups.get((ctx.guild.id, ctx.channel.id))
        if signup is not None:
            if member.id not in signup.entrants:
                await ctx.send("That user is not in the current signup/ready-check.", delete_after=10)
                return
            was_ready = member.id in signup.ready_confirmed
            signup.entrants.discard(member.id)
            signup.ready_confirmed.discard(member.id)
            await self._remove_entry(signup.tournament_id, member.id)
            if signup.status == "ready_check" and not was_ready:
                await self._record_no_show(ctx.guild.id, member.id, readycheck_missed=1)
            await self._save_signup_runtime(signup)
            await self._refresh_signup_message(signup)
            if signup.status == "ready_check":
                if len(signup.entrants) < 2 or signup.ready_confirmed == signup.entrants:
                    await self._finalize_ready_check(signup)
                await ctx.send(f"🚫 Removed {member.mention} from the current tournament ready-check.")
            else:
                await ctx.send(f"🚫 Removed {member.mention} from the current tournament signup.")
            return

        tournament = self._get_live_tournament(ctx.guild.id, ctx.channel.id)
        if tournament is None:
            await ctx.send("There is no live duel tournament in this channel.", delete_after=10)
            return
        if tournament.status == "paused":
            await ctx.send("Resume the tournament before using `dq` on an active match.", delete_after=10)
            return
        if member.id in tournament.next_round_players:
            tournament.next_round_players = [uid for uid in tournament.next_round_players if uid != member.id]
            await self._save_tournament_runtime(tournament)
            await ctx.send(f"🚫 Disqualified {member.mention} from the next round queue.")
            return
        match, error = self._resolve_moderation_match(tournament, channel=ctx.channel, target_user_id=member.id)
        if match is None:
            await ctx.send(error or "Could not find that player's active match.", delete_after=12)
            return
        winner_user_id = match.player1_user_id if match.player2_user_id == member.id else match.player2_user_id
        await self._admin_finish_match(
            match,
            winner_user_id,
            reason=f"{member.display_name} was disqualified by a moderator",
            penalize_user_id=member.id,
        )
        await ctx.send(f"🚫 Disqualified {member.mention}. Their opponent advances.")

    @dueltourney.command(name="advance")
    async def dueltourney_advance(self, ctx: commands.Context, member: discord.Member) -> None:
        if not ctx.guild:
            return
        if not self._can_manage(ctx.author):
            await ctx.send("Only moderators can force-advance players.", delete_after=10)
            return
        tournament = self._get_live_tournament(ctx.guild.id, ctx.channel.id)
        if tournament is None:
            await ctx.send("There is no live duel tournament in this channel.", delete_after=10)
            return
        if tournament.status == "paused":
            await ctx.send("Resume the tournament before force-advancing a player.", delete_after=10)
            return
        match, error = self._resolve_moderation_match(tournament, channel=ctx.channel, target_user_id=member.id)
        if match is None:
            await ctx.send(error or "Could not find that player's active match.", delete_after=12)
            return
        if member.id not in {match.player1_user_id, match.player2_user_id}:
            await ctx.send("That user is not one of the two players in the selected match.", delete_after=10)
            return
        await ctx.send(f"🏁 Advanced {member.mention} to the next round.")
        await self._admin_finish_match(match, member.id, reason=f"{member.display_name} was advanced by a moderator")

    @dueltourney.command(name="remake")
    async def dueltourney_remake(self, ctx: commands.Context, member: discord.Member | None = None) -> None:
        if not ctx.guild:
            return
        if not self._can_manage(ctx.author):
            await ctx.send("Only moderators can remake a match.", delete_after=10)
            return
        tournament = self._get_live_tournament(ctx.guild.id, ctx.channel.id)
        if tournament is None:
            await ctx.send("There is no live duel tournament in this channel.", delete_after=10)
            return
        if tournament.status == "paused":
            await ctx.send("Resume the tournament before remaking a match.", delete_after=10)
            return
        match, error = self._resolve_moderation_match(
            tournament,
            channel=ctx.channel,
            target_user_id=member.id if member is not None else None,
        )
        if match is None:
            await ctx.send(error or "Could not identify the match to remake.", delete_after=12)
            return
        await self._admin_remake_match(match, reason=f"Remade by {ctx.author.display_name}")
        await ctx.send("🔁 Remade the selected match from scratch.")

    @dueltourney.command(name="forceend")
    async def dueltourney_forceend(self, ctx: commands.Context, member: discord.Member | None = None) -> None:
        if not ctx.guild:
            return
        if not self._can_manage(ctx.author):
            await ctx.send("Only moderators can force-end a tournament.", delete_after=10)
            return
        tournament = self._get_live_tournament(ctx.guild.id, ctx.channel.id)
        if tournament is None:
            await ctx.send("There is no live duel tournament in this channel.", delete_after=10)
            return
        if member is None:
            await ctx.send("Use `!dueltourney forceend @user` to award the title immediately, or `!dueltourney cancel` to cancel the whole event.", delete_after=15)
            return
        if member.id not in tournament.entrants:
            await ctx.send("That user is not part of this tournament.", delete_after=10)
            return
        await self._admin_finish_tournament(tournament, winner_user_id=member.id, reason=f"Forced by {ctx.author.display_name}")
        await ctx.send(f"🏆 Force-ended the tournament and awarded the title to {member.mention}.")

    @dueltourney.command(name="status")
    async def dueltourney_status(self, ctx: commands.Context) -> None:
        if not ctx.guild:
            return
        signup = self._signups.get((ctx.guild.id, ctx.channel.id))
        if signup:
            await ctx.send(embed=self._build_signup_embed(signup, ctx.guild))
            return
        active = self._get_live_tournament(ctx.guild.id, ctx.channel.id)
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
            await self._cancel_auto_signup_task(ctx.guild.id, ctx.channel.id)
            await self._cancel_ready_check_task(ctx.guild.id, ctx.channel.id)
            await self._set_tournament_status(signup.tournament_id, "cancelled")
            await self._delete_signup_runtime(signup)
            await self._disable_signup_message(signup, ctx.guild, new_title=f"{signup.title} • Cancelled")
            await ctx.send("Cancelled the signup.")
            return
        active = self._get_live_tournament(ctx.guild.id, ctx.channel.id)
        if not active:
            await ctx.send("There is no active duel tournament in this channel.", delete_after=10)
            return
        active.status = "cancelled"
        await self._set_tournament_status(active.tournament_id, "cancelled")
        for match_id in list(active.active_match_ids):
            task = self._match_tasks.pop(match_id, None)
            if task and not task.done():
                task.cancel()
            await self._delete_match_runtime(match_id)
            self._matches.pop(match_id, None)
        await self._delete_tournament_runtime(active)
        await ctx.send("Cancelled the active duel tournament.")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(DuelTournamentCog(bot))
