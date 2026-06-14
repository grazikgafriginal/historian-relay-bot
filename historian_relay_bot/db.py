from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Tuple

import aiosqlite
import re

log = logging.getLogger("historian_relay.db")

@dataclass(slots=True)
class QuestionRow:
    id: int
    guild_id: str
    created_by_user_id: str
    question_text: str
    tag: Optional[str]
    era: Optional[str]
    status: str
    created_at: int
    updated_at: int
    origin_channel_id: str
    origin_message_id: str
    origin_thread_id: Optional[str]
    queue_message_id: Optional[str]
    hist_message_id: Optional[str]
    claimed_by_user_id: Optional[str]
    claimed_at: Optional[int]
    answer_text: Optional[str]
    answered_by_user_id: Optional[str]
    answered_at: Optional[int]

def now_ts() -> int:
    return int(time.time())

class Database:
    def __init__(self, path: str):
        self.path = path
        self._conn: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._conn is not None
        return self._conn



    async def connect(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL;")
        await self._conn.execute("PRAGMA foreign_keys=ON;")
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def init_schema(self, schema_path: str) -> None:
        if not self._conn:
            raise RuntimeError("DB not connected.")

        sql = Path(schema_path).read_text(encoding="utf-8")

        # The GuessYear feature should only enforce "one ACTIVE round per channel".
        # Older schema versions used a strict UNIQUE index on (guild_id, channel_id),
        # which breaks as soon as you have multiple historical rounds in the same channel.
        sql = self._sanitize_schema_sql(sql)
        # Prevent UNIQUE index creation from failing due to stale duplicate 'active' rounds.
        await self._pre_schema_guessyear_cleanup()


        await self._conn.executescript(sql)
        await self._conn.commit()

        # Post-migration guardrails (idempotent).
        await self._ensure_guessyear_constraints()



    @staticmethod
    def _sanitize_schema_sql(sql: str) -> str:
        """Remove the *wrong* GuessYear unique index from schema.sql.

        The GuessYear feature only needs **one ACTIVE round per channel**. A strict
        UNIQUE index on (guild_id, channel_id) breaks history (you can't store more
        than one round per channel) and may prevent the bot from starting if the DB
        already contains historical rounds.

        We intentionally do NOT remove a partial unique index like:
            CREATE UNIQUE INDEX ... ON guessyear_rounds(guild_id, channel_id) WHERE status='active';
        """
        # Match both (guild_id, channel_id) and (channel_id, guild_id), possibly multi-line.
        patterns = [
            r"(?is)\bCREATE\s+UNIQUE\s+INDEX\b[^;]*?\bON\s+guessyear_rounds\s*\(\s*guild_id\s*,\s*channel_id\s*\)[^;]*?;\s*",
            r"(?is)\bCREATE\s+UNIQUE\s+INDEX\b[^;]*?\bON\s+guessyear_rounds\s*\(\s*channel_id\s*,\s*guild_id\s*\)[^;]*?;\s*",
        ]

        removed = False

        def repl(m: re.Match) -> str:
            nonlocal removed
            stmt = m.group(0)
            # Keep partial indexes.
            if re.search(r"\bWHERE\b", stmt, flags=re.IGNORECASE):
                return stmt
            removed = True
            return "\n"

        for pat in patterns:
            sql = re.sub(pat, repl, sql)

        if removed:
            log.warning(
                "Schema contained an incompatible GuessYear UNIQUE index; it was removed and will be replaced with a partial unique index (active rounds only)."  # noqa: E501
            )
        return sql


    async def _pre_schema_guessyear_cleanup(self) -> None:
        """Best-effort cleanup so schema migrations don't crash on existing DBs.

        If schema.sql contains a UNIQUE index (strict or partial), it can fail when the DB
        already has multiple ACTIVE rounds for the same channel (e.g., after crashes).
        This runs before executing schema.sql and ends expired/duplicate active rounds.
        """
        assert self._conn

        row = await self.fetchone("SELECT name FROM sqlite_master WHERE type='table' AND name='guessyear_rounds'")
        if not row:
            return

        # Ensure required columns exist before we touch them.
        async with self._conn.execute("PRAGMA table_info('guessyear_rounds')") as cur:
            cols = {str(r[1]) for r in await cur.fetchall()}

        if "status" not in cols:
            await self._conn.execute("ALTER TABLE guessyear_rounds ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
        if "hints_used" not in cols:
            await self._conn.execute("ALTER TABLE guessyear_rounds ADD COLUMN hints_used INTEGER NOT NULL DEFAULT 0")

        now = int(time.time())
        await self._conn.execute(
            "UPDATE guessyear_rounds SET status='ended' WHERE status='active' AND ends_at<=?",
            (now,),
        )

        # End duplicate ACTIVE rounds per (guild_id, channel_id), keeping newest round_id.
        async with self._conn.execute(
            "SELECT round_id, guild_id, channel_id FROM guessyear_rounds WHERE status='active' ORDER BY round_id DESC"
        ) as cur:
            active = await cur.fetchall()

        seen: set[tuple[str, str]] = set()
        to_end: list[int] = []
        for r in active:
            key = (str(r[1]), str(r[2]))
            if key in seen:
                to_end.append(int(r[0]))
            else:
                seen.add(key)

        if to_end:
            log.warning("Ending %d duplicate active GuessYear rounds (pre-schema cleanup)", len(to_end))
            await self._conn.executemany(
                "UPDATE guessyear_rounds SET status='ended' WHERE round_id=?",
                [(rid,) for rid in to_end],
            )

        await self._conn.commit()

    async def _ensure_guessyear_constraints(self) -> None:
        """Idempotent post-schema migration for GuessYear tables."""
        assert self._conn

        # If the GuessYear tables aren't present, nothing to do.
        row = await self.fetchone(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='guessyear_rounds'"
        )
        if not row:
            return

        # If guessyear_rounds has a table-level UNIQUE constraint on (guild_id, channel_id),
        # SQLite creates an autoindex we cannot drop. Rebuild the table to remove it.
        strict_autoindex = False

        async with self._conn.execute("PRAGMA index_list('guessyear_rounds')") as cur:
            idxs = await cur.fetchall()

        for idx in idxs:
            # With Row factory, columns are: seq, name, unique, origin, partial
            name = str(idx[1])
            unique = int(idx[2])
            partial = int(idx[4])

            if unique != 1 or partial == 1:
                continue

            async with self._conn.execute(f"PRAGMA index_info('{name}')") as cur:
                cols = [r[2] for r in await cur.fetchall()]

            if cols not in (["guild_id", "channel_id"], ["channel_id", "guild_id"]):
                continue

            if name.startswith("sqlite_autoindex_"):
                strict_autoindex = True
            else:
                log.warning("Dropping incompatible unique index %s on guessyear_rounds", name)
                await self._conn.execute(f'DROP INDEX IF EXISTS "{name}"')

        if strict_autoindex:
            await self._rebuild_guessyear_rounds_table()

        # End any duplicate ACTIVE rounds (keep the most recent per channel).
        async with self._conn.execute(
            """
            SELECT round_id, guild_id, channel_id, started_at, ends_at
            FROM guessyear_rounds
            WHERE status='active'
            ORDER BY started_at DESC, round_id DESC
            """
        ) as cur:
            active = await cur.fetchall()

        seen: set[tuple[str, str]] = set()
        to_end: list[int] = []
        for r in active:
            key = (str(r[1]), str(r[2]))
            if key in seen:
                to_end.append(int(r[0]))
            else:
                seen.add(key)

        if to_end:
            log.warning("Ending %d duplicate active GuessYear rounds", len(to_end))
            await self._conn.executemany(
                "UPDATE guessyear_rounds SET status='ended' WHERE round_id=?",
                [(rid,) for rid in to_end],
            )

        # Enforce: at most one ACTIVE round per (guild_id, channel_id).
        await self._conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_guessyear_active_round
            ON guessyear_rounds(guild_id, channel_id)
            WHERE status='active'
            """
        )

        # Migrate guessyear_stats: add new columns if missing.
        stats_row = await self.fetchone(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='guessyear_stats'"
        )
        if stats_row:
            async with self._conn.execute("PRAGMA table_info('guessyear_stats')") as cur:
                stats_cols = {str(r[1]) for r in await cur.fetchall()}
            new_cols = {
                "exact_hits": "INTEGER NOT NULL DEFAULT 0",
                "current_streak": "INTEGER NOT NULL DEFAULT 0",
                "best_streak": "INTEGER NOT NULL DEFAULT 0",
                "total_distance": "INTEGER NOT NULL DEFAULT 0",
                "duel_wins": "INTEGER NOT NULL DEFAULT 0",
                "duel_losses": "INTEGER NOT NULL DEFAULT 0",
                "xp": "INTEGER NOT NULL DEFAULT 0",
            }
            for col_name, col_def in new_cols.items():
                if col_name not in stats_cols:
                    await self._conn.execute(f"ALTER TABLE guessyear_stats ADD COLUMN {col_name} {col_def}")

        # Create channel categories table if missing.
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS guessyear_channel_categories (
              guild_id TEXT NOT NULL,
              channel_id TEXT NOT NULL,
              categories_json TEXT NOT NULL DEFAULT '[]',
              PRIMARY KEY (guild_id, channel_id)
            )
            """
        )

        # Create duel matchups table if missing.
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS guessyear_duel_matchups (
              guild_id TEXT NOT NULL,
              user_a TEXT NOT NULL,
              user_b TEXT NOT NULL,
              wins_a INTEGER NOT NULL DEFAULT 0,
              wins_b INTEGER NOT NULL DEFAULT 0,
              PRIMARY KEY (guild_id, user_a, user_b)
            )
            """
        )

        # Create achievements table if missing.
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS guessyear_achievements (
              guild_id TEXT NOT NULL,
              user_id TEXT NOT NULL,
              achievement_key TEXT NOT NULL,
              earned_at INTEGER NOT NULL,
              PRIMARY KEY (guild_id, user_id, achievement_key)
            )
            """
        )

        await self._conn.commit()

    async def _rebuild_guessyear_rounds_table(self) -> None:
        """Rebuild guessyear_rounds to remove a too-strict table-level UNIQUE constraint."""
        assert self._conn
        log.warning("Rebuilding guessyear_rounds to remove incompatible UNIQUE constraint…")

        # Some older DBs might not have the hints_used column yet.
        async with self._conn.execute("PRAGMA table_info('guessyear_rounds')") as cur:
            cols = [r[1] for r in await cur.fetchall()]
        has_hints = "hints_used" in set(map(str, cols))
        hints_expr = "COALESCE(hints_used, 0)" if has_hints else "0"

        await self._conn.executescript(
            f"""
            PRAGMA foreign_keys=OFF;
            BEGIN;

            CREATE TABLE IF NOT EXISTS guessyear_rounds__new (
                round_id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                started_by_user_id TEXT NOT NULL,
                event_id TEXT NOT NULL,
                correct_year INTEGER NOT NULL,
                started_at INTEGER NOT NULL,
                ends_at INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                hints_used INTEGER NOT NULL DEFAULT 0
            );

            INSERT INTO guessyear_rounds__new
            (round_id, guild_id, channel_id, started_by_user_id, event_id, correct_year, started_at, ends_at, status, hints_used)
            SELECT
                round_id, guild_id, channel_id, started_by_user_id, event_id, correct_year, started_at, ends_at, status,
                {hints_expr}
            FROM guessyear_rounds;

            DROP TABLE guessyear_rounds;
            ALTER TABLE guessyear_rounds__new RENAME TO guessyear_rounds;

            COMMIT;
            PRAGMA foreign_keys=ON;
            """
        )
        await self._conn.commit()

    async def fetchone(self, sql: str, params: Tuple[Any, ...] = ()) -> Optional[aiosqlite.Row]:
        assert self._conn
        async with self._conn.execute(sql, params) as cur:
            return await cur.fetchone()

    async def fetchall(self, sql: str, params: Tuple[Any, ...] = ()) -> list[aiosqlite.Row]:
        assert self._conn
        async with self._conn.execute(sql, params) as cur:
            return await cur.fetchall()

    async def execute(self, sql: str, params: Tuple[Any, ...] = ()) -> None:
        assert self._conn
        await self._conn.execute(sql, params)

    async def commit(self) -> None:
        assert self._conn
        await self._conn.commit()

    # --------------------------
    # Questions
    # --------------------------

    async def create_question(
        self,
        *,
        guild_id: int,
        created_by_user_id: int,
        question_text: str,
        tag: Optional[str],
        era: Optional[str],
        status: str,
        origin_channel_id: int,
        origin_message_id: int,
        origin_thread_id: Optional[int],
    ) -> int:
        ts = now_ts()
        assert self._conn

        async with self._lock:
            cur = await self._conn.execute(
                """
                INSERT INTO questions(
                  guild_id, created_by_user_id, question_text, tag, era, status,
                  created_at, updated_at,
                  origin_channel_id, origin_message_id, origin_thread_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(guild_id),
                    str(created_by_user_id),
                    question_text,
                    tag,
                    era,
                    status,
                    ts,
                    ts,
                    str(origin_channel_id),
                    str(origin_message_id),
                    str(origin_thread_id) if origin_thread_id else None,
                ),
            )
            await self._conn.commit()
            return int(cur.lastrowid)

    async def get_question(self, guild_id: int, qid: int) -> Optional[QuestionRow]:
        row = await self.fetchone(
            "SELECT * FROM questions WHERE guild_id=? AND id=?",
            (str(guild_id), qid),
        )
        return QuestionRow(**dict(row)) if row else None

    async def set_question_message_refs(
        self,
        guild_id: int,
        qid: int,
        *,
        queue_message_id: Optional[int] = None,
        hist_message_id: Optional[int] = None,
        origin_thread_id: Optional[int] = None,
    ) -> None:
        ts = now_ts()
        await self.execute(
            """
            UPDATE questions
            SET queue_message_id=COALESCE(?, queue_message_id),
                hist_message_id=COALESCE(?, hist_message_id),
                origin_thread_id=COALESCE(?, origin_thread_id),
                updated_at=?
            WHERE guild_id=? AND id=?
            """,
            (
                str(queue_message_id) if queue_message_id else None,
                str(hist_message_id) if hist_message_id else None,
                str(origin_thread_id) if origin_thread_id else None,
                ts,
                str(guild_id),
                qid,
            ),
        )
        await self.commit()

    async def update_status(self, guild_id: int, qid: int, status: str) -> None:
        ts = now_ts()
        await self.execute(
            "UPDATE questions SET status=?, updated_at=? WHERE guild_id=? AND id=?",
            (status, ts, str(guild_id), qid),
        )
        await self.commit()

    async def try_claim(self, guild_id: int, qid: int, user_id: int) -> bool:
        """
        First-come-first-serve claim. Uses an IMMEDIATE transaction.
        """
        assert self._conn
        async with self._lock:
            await self._conn.execute("BEGIN IMMEDIATE;")
            row = await self.fetchone(
                "SELECT status, claimed_by_user_id FROM questions WHERE guild_id=? AND id=?",
                (str(guild_id), qid),
            )
            if not row:
                await self._conn.execute("ROLLBACK;")
                return False

            status = row["status"]
            claimed_by = row["claimed_by_user_id"]
            if status not in ("pending", "claimed", "needs_context") or claimed_by:
                await self._conn.execute("ROLLBACK;")
                return False

            ts = now_ts()
            await self._conn.execute(
                """
                UPDATE questions
                SET status='claimed',
                    claimed_by_user_id=?,
                    claimed_at=?,
                    updated_at=?
                WHERE guild_id=? AND id=? AND claimed_by_user_id IS NULL
                """,
                (str(user_id), ts, ts, str(guild_id), qid),
            )
            await self._conn.commit()
            return True

    async def unclaim(self, guild_id: int, qid: int) -> None:
        ts = now_ts()
        await self.execute(
            """
            UPDATE questions
            SET status='pending',
                claimed_by_user_id=NULL,
                claimed_at=NULL,
                updated_at=?
            WHERE guild_id=? AND id=?
            """,
            (ts, str(guild_id), qid),
        )
        await self.commit()

    async def set_answer(
        self,
        guild_id: int,
        qid: int,
        *,
        answer_text: str,
        answered_by_user_id: int,
    ) -> None:
        ts = now_ts()
        await self.execute(
            """
            UPDATE questions
            SET status='answered',
                answer_text=?,
                answered_by_user_id=?,
                answered_at=?,
                updated_at=?
            WHERE guild_id=? AND id=?
            """,
            (answer_text, str(answered_by_user_id), ts, ts, str(guild_id), qid),
        )
        await self.commit()

    async def find_by_hist_message(self, guild_id: int, hist_message_id: int) -> Optional[QuestionRow]:
        row = await self.fetchone(
            "SELECT * FROM questions WHERE guild_id=? AND hist_message_id=?",
            (str(guild_id), str(hist_message_id)),
        )
        return QuestionRow(**dict(row)) if row else None

    async def find_by_queue_message(self, guild_id: int, queue_message_id: int) -> Optional[QuestionRow]:
        row = await self.fetchone(
            "SELECT * FROM questions WHERE guild_id=? AND queue_message_id=?",
            (str(guild_id), str(queue_message_id)),
        )
        return QuestionRow(**dict(row)) if row else None

    async def list_messages_to_restore(self, guild_id: int) -> dict[str, list[QuestionRow]]:
        """
        Returns questions that have message IDs stored, for reattaching views after restart.
        """
        rows_hist = await self.fetchall(
            """
            SELECT * FROM questions
            WHERE guild_id=? AND hist_message_id IS NOT NULL
              AND status IN ('pending','claimed','needs_context','answered')
            """,
            (str(guild_id),),
        )
        rows_queue = await self.fetchall(
            """
            SELECT * FROM questions
            WHERE guild_id=? AND queue_message_id IS NOT NULL
              AND status IN ('queued')
            """,
            (str(guild_id),),
        )
        return {
            "hist": [QuestionRow(**dict(r)) for r in rows_hist],
            "queue": [QuestionRow(**dict(r)) for r in rows_queue],
        }

    # --------------------------
    # Cooldowns / blacklist
    # --------------------------

    async def is_blacklisted(self, guild_id: int, user_id: int) -> Optional[str]:
        row = await self.fetchone(
            "SELECT reason FROM blacklist WHERE guild_id=? AND user_id=?",
            (str(guild_id), str(user_id)),
        )
        return row["reason"] if row else None

    async def set_blacklist(self, guild_id: int, user_id: int, reason: str | None, added_by_user_id: int) -> None:
        ts = now_ts()
        await self.execute(
            """
            INSERT INTO blacklist(guild_id, user_id, reason, added_by_user_id, added_at)
            VALUES(?,?,?,?,?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
              reason=excluded.reason,
              added_by_user_id=excluded.added_by_user_id,
              added_at=excluded.added_at
            """,
            (str(guild_id), str(user_id), reason, str(added_by_user_id), ts),
        )
        await self.commit()

    async def remove_blacklist(self, guild_id: int, user_id: int) -> None:
        await self.execute(
            "DELETE FROM blacklist WHERE guild_id=? AND user_id=?",
            (str(guild_id), str(user_id)),
        )
        await self.commit()

    async def get_cooldown(self, guild_id: int, user_id: int) -> Optional[aiosqlite.Row]:
        return await self.fetchone(
            "SELECT * FROM cooldowns WHERE guild_id=? AND user_id=?",
            (str(guild_id), str(user_id)),
        )

    async def upsert_cooldown(self, guild_id: int, user_id: int, *, last_asked_at: int, daily_count: int, daily_reset_at: int) -> None:
        await self.execute(
            """
            INSERT INTO cooldowns(guild_id, user_id, last_asked_at, daily_count, daily_reset_at)
            VALUES(?,?,?,?,?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
              last_asked_at=excluded.last_asked_at,
              daily_count=excluded.daily_count,
              daily_reset_at=excluded.daily_reset_at
            """,
            (str(guild_id), str(user_id), last_asked_at, daily_count, daily_reset_at),
        )
        await self.commit()

        ### GUESS THE YEAR BOT

    async def guessyear_create_round(self, guild_id: int, channel_id: int, started_by_user_id: int,
                                     event_id: str, correct_year: int, started_at: int, ends_at: int) -> int:
        q = """
        INSERT INTO guessyear_rounds
        (guild_id, channel_id, started_by_user_id, event_id, correct_year, started_at, ends_at, status, hints_used)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'active', 0)
        """
        cur = await self.conn.execute(q, (str(guild_id), str(channel_id), str(started_by_user_id), event_id,
                                         int(correct_year), int(started_at), int(ends_at)))
        await self.conn.commit()
        return cur.lastrowid

    async def guessyear_get_active_round(self, guild_id: int, channel_id: int, now_ts: int):
        q = """
        SELECT round_id, guild_id, channel_id, event_id, correct_year, started_at, ends_at, status, hints_used
        FROM guessyear_rounds
        WHERE guild_id=? AND channel_id=? AND status='active' AND ends_at>?
        ORDER BY round_id DESC
        LIMIT 1
        """
        cur = await self.conn.execute(q, (str(guild_id), str(channel_id), int(now_ts)))
        row = await cur.fetchone()
        if not row:
            return None
        cols = [c[0] for c in cur.description]
        return dict(zip(cols, row))

    async def guessyear_list_active_rounds(self, now_ts: int):
        q = """
        SELECT round_id, guild_id, channel_id, event_id, correct_year, started_at, ends_at, status, hints_used
        FROM guessyear_rounds
        WHERE status='active' AND ends_at>?
        """
        cur = await self._conn.execute(q, (int(now_ts),))
        rows = await cur.fetchall()
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in rows]

    async def guessyear_mark_round_ended(self, round_id: int) -> None:
        await self.conn.execute(
            "UPDATE guessyear_rounds SET status='ended' WHERE round_id=? AND status='active'",
            (int(round_id),),
        )
        await self.conn.commit()

    async def guessyear_mark_round_cancelled(self, round_id: int) -> None:
        await self.conn.execute(
            "UPDATE guessyear_rounds SET status='cancelled' WHERE round_id=? AND status='active'",
            (int(round_id),),
        )
        await self.conn.commit()

    async def guessyear_increment_hints_used(self, round_id: int) -> int:
        await self.conn.execute(
            "UPDATE guessyear_rounds SET hints_used = hints_used + 1 WHERE round_id=?",
            (int(round_id),),
        )
        await self.conn.commit()
        cur = await self.conn.execute("SELECT hints_used FROM guessyear_rounds WHERE round_id=?", (int(round_id),))
        row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def guessyear_upsert_guess(self, round_id: int, user_id: int, guess_year: int, guessed_at: int, policy: str):
        """
        Returns (ok, existing_guess_year_or_None)
        """
        cur = await self.conn.execute(
            "SELECT guess_year FROM guessyear_guesses WHERE round_id=? AND user_id=?",
            (int(round_id), str(user_id)),
        )
        existing = await cur.fetchone()
        already = int(existing[0]) if existing is not None else None

        if already is not None and policy == "first":
            return True, already

        if policy == "latest":
            q = """
            INSERT INTO guessyear_guesses (round_id, user_id, guess_year, guessed_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(round_id, user_id) DO UPDATE SET
              guess_year=excluded.guess_year,
              guessed_at=excluded.guessed_at
            """
            await self.conn.execute(q, (int(round_id), str(user_id), int(guess_year), int(guessed_at)))
        else:
            # first
            await self.conn.execute(
                "INSERT OR IGNORE INTO guessyear_guesses (round_id, user_id, guess_year, guessed_at) VALUES (?, ?, ?, ?)",
                (int(round_id), str(user_id), int(guess_year), int(guessed_at)),
            )

        await self.conn.commit()
        return True, already

    async def guessyear_list_guesses(self, round_id: int):
        q = """
        SELECT round_id, user_id, guess_year, guessed_at
        FROM guessyear_guesses
        WHERE round_id=?
        """
        cur = await self.conn.execute(q, (int(round_id),))
        rows = await cur.fetchall()
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in rows]

    # --- optional stats ---
    async def guessyear_stats_record_play(self, guild_id: int, user_ids: list[int]) -> None:
        now = int(time.time())
        for uid in set(user_ids):
            await self.conn.execute(
                """
                INSERT INTO guessyear_stats (guild_id, user_id, wins, plays, exact_hits, current_streak,
                    best_streak, total_distance, duel_wins, duel_losses, xp, last_played_at)
                VALUES (?, ?, 0, 1, 0, 0, 0, 0, 0, 0, 0, ?)
                ON CONFLICT(guild_id, user_id) DO UPDATE SET
                  plays = plays + 1,
                  last_played_at = excluded.last_played_at
                """,
                (str(guild_id), str(uid), now),
            )
        await self.conn.commit()

    async def guessyear_stats_record_win(self, guild_id: int, user_id: int) -> None:
        now = int(time.time())
        await self.conn.execute(
            """
            INSERT INTO guessyear_stats (guild_id, user_id, wins, plays, exact_hits, current_streak,
                best_streak, total_distance, duel_wins, duel_losses, xp, last_played_at)
            VALUES (?, ?, 1, 0, 0, 1, 1, 0, 0, 0, 0, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
              wins = wins + 1,
              last_played_at = excluded.last_played_at
            """,
            (str(guild_id), str(user_id), now),
        )
        await self.conn.commit()

    async def guessyear_stats_get_top(self, guild_id: int, limit: int = 10) -> list[dict[str, Any]]:
        """Return top GuessYear stats rows for a guild.

        Ordering favors wins, then plays, then recency.
        """
        q = """
        SELECT user_id, wins, plays, exact_hits, current_streak, best_streak,
               total_distance, duel_wins, duel_losses, xp, last_played_at
        FROM guessyear_stats
        WHERE guild_id=?
        ORDER BY wins DESC, plays DESC, last_played_at DESC
        LIMIT ?
        """
        cur = await self.conn.execute(q, (str(guild_id), int(limit)))
        rows = await cur.fetchall()
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in rows]

    async def guessyear_stats_get_user(self, guild_id: int, user_id: int) -> Optional[dict[str, Any]]:
        """Return a single user's GuessYear stats plus rank + total.

        Uses window functions (available in modern SQLite).
        """
        q = """
        WITH ranked AS (
          SELECT
            user_id,
            wins,
            plays,
            exact_hits,
            current_streak,
            best_streak,
            total_distance,
            duel_wins,
            duel_losses,
            xp,
            last_played_at,
            RANK() OVER (ORDER BY wins DESC, plays DESC, last_played_at DESC) AS rank,
            COUNT(*) OVER () AS total
          FROM guessyear_stats
          WHERE guild_id=?
        )
        SELECT user_id, wins, plays, exact_hits, current_streak, best_streak,
               total_distance, duel_wins, duel_losses, xp, last_played_at, rank, total
        FROM ranked
        WHERE user_id=?
        """
        cur = await self.conn.execute(q, (str(guild_id), str(user_id)))
        row = await cur.fetchone()
        if not row:
            return None
        cols = [c[0] for c in cur.description]
        return dict(zip(cols, row))

    async def guessyear_try_end_round(self, round_id: int) -> bool:
        """
        Returns True only for the first caller that successfully ends the round.
        All later callers get False (prevents duplicate end messages).
        """
        async with self._lock:
            cur = await self._conn.execute(
                """
                UPDATE guessyear_rounds
                SET status='ended'
                WHERE round_id=? AND status='active'
                """,
                (int(round_id),),
            )
            await self._conn.commit()
            return (cur.rowcount or 0) > 0

    # --- category persistence ---

    async def guessyear_get_channel_categories(self, guild_id: int, channel_id: int) -> Optional[str]:
        cur = await self.conn.execute(
            "SELECT categories_json FROM guessyear_channel_categories WHERE guild_id=? AND channel_id=?",
            (str(guild_id), str(channel_id)),
        )
        row = await cur.fetchone()
        return str(row[0]) if row else None

    async def guessyear_set_channel_categories(self, guild_id: int, channel_id: int, categories_json: str) -> None:
        await self.conn.execute(
            """
            INSERT INTO guessyear_channel_categories (guild_id, channel_id, categories_json)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id, channel_id) DO UPDATE SET categories_json=excluded.categories_json
            """,
            (str(guild_id), str(channel_id), categories_json),
        )
        await self.conn.commit()

    async def guessyear_delete_channel_categories(self, guild_id: int, channel_id: int) -> None:
        await self.conn.execute(
            "DELETE FROM guessyear_channel_categories WHERE guild_id=? AND channel_id=?",
            (str(guild_id), str(channel_id)),
        )
        await self.conn.commit()

    async def guessyear_load_all_channel_categories(self) -> list[dict[str, Any]]:
        cur = await self.conn.execute("SELECT guild_id, channel_id, categories_json FROM guessyear_channel_categories")
        rows = await cur.fetchall()
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in rows]

    # --- extended stats ---

    async def guessyear_stats_record_exact_hit(self, guild_id: int, user_id: int) -> None:
        await self.conn.execute(
            """
            UPDATE guessyear_stats SET exact_hits = exact_hits + 1
            WHERE guild_id=? AND user_id=?
            """,
            (str(guild_id), str(user_id)),
        )
        await self.conn.commit()

    async def guessyear_stats_record_distance(self, guild_id: int, user_id: int, distance: int) -> None:
        await self.conn.execute(
            """
            UPDATE guessyear_stats SET total_distance = total_distance + ?
            WHERE guild_id=? AND user_id=?
            """,
            (int(distance), str(guild_id), str(user_id)),
        )
        await self.conn.commit()

    async def guessyear_stats_update_streak(self, guild_id: int, user_id: int, won: bool) -> None:
        if won:
            await self.conn.execute(
                """
                UPDATE guessyear_stats
                SET current_streak = current_streak + 1,
                    best_streak = MAX(best_streak, current_streak + 1)
                WHERE guild_id=? AND user_id=?
                """,
                (str(guild_id), str(user_id)),
            )
        else:
            await self.conn.execute(
                "UPDATE guessyear_stats SET current_streak = 0 WHERE guild_id=? AND user_id=?",
                (str(guild_id), str(user_id)),
            )
        await self.conn.commit()

    async def guessyear_stats_record_duel_result(self, guild_id: int, winner_id: int, loser_id: int) -> None:
        now = int(time.time())
        for uid, w, l in [(winner_id, 1, 0), (loser_id, 0, 1)]:
            await self.conn.execute(
                """
                INSERT INTO guessyear_stats (guild_id, user_id, wins, plays, exact_hits, current_streak,
                    best_streak, total_distance, duel_wins, duel_losses, xp, last_played_at)
                VALUES (?, ?, 0, 0, 0, 0, 0, 0, ?, ?, 0, ?)
                ON CONFLICT(guild_id, user_id) DO UPDATE SET
                  duel_wins = duel_wins + ?,
                  duel_losses = duel_losses + ?,
                  last_played_at = excluded.last_played_at
                """,
                (str(guild_id), str(uid), w, l, now, w, l),
            )
        await self.conn.commit()

    async def guessyear_stats_add_xp(self, guild_id: int, user_id: int, amount: int) -> int:
        now = int(time.time())
        await self.conn.execute(
            """
            INSERT INTO guessyear_stats (guild_id, user_id, wins, plays, exact_hits, current_streak,
                best_streak, total_distance, duel_wins, duel_losses, xp, last_played_at)
            VALUES (?, ?, 0, 0, 0, 0, 0, 0, 0, 0, ?, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
              xp = xp + ?,
              last_played_at = excluded.last_played_at
            """,
            (str(guild_id), str(user_id), int(amount), now, int(amount)),
        )
        await self.conn.commit()
        cur = await self.conn.execute(
            "SELECT xp FROM guessyear_stats WHERE guild_id=? AND user_id=?",
            (str(guild_id), str(user_id)),
        )
        row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def guessyear_record_duel_matchup(self, guild_id: int, winner_id: int, loser_id: int) -> None:
        a, b = sorted([str(winner_id), str(loser_id)])
        win_col = "wins_a" if a == str(winner_id) else "wins_b"
        await self.conn.execute(
            f"""
            INSERT INTO guessyear_duel_matchups (guild_id, user_a, user_b, wins_a, wins_b)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, user_a, user_b) DO UPDATE SET
              {win_col} = {win_col} + 1
            """,
            (str(guild_id), a, b, 1 if win_col == "wins_a" else 0, 1 if win_col == "wins_b" else 0),
        )
        await self.conn.commit()

    async def guessyear_get_duel_matchup(self, guild_id: int, user1: int, user2: int) -> tuple[int, int]:
        a, b = sorted([str(user1), str(user2)])
        cur = await self.conn.execute(
            "SELECT wins_a, wins_b FROM guessyear_duel_matchups WHERE guild_id=? AND user_a=? AND user_b=?",
            (str(guild_id), a, b),
        )
        row = await cur.fetchone()
        if not row:
            return (0, 0)
        wa, wb = int(row[0]), int(row[1])
        if a == str(user1):
            return (wa, wb)
        return (wb, wa)

    async def guessyear_get_achievements(self, guild_id: int, user_id: int) -> set:
        cur = await self.conn.execute(
            "SELECT achievement_key FROM guessyear_achievements WHERE guild_id=? AND user_id=?",
            (str(guild_id), str(user_id)),
        )
        rows = await cur.fetchall()
        return {str(r[0]) for r in rows}

    async def guessyear_grant_achievement(self, guild_id: int, user_id: int, key: str) -> bool:
        now = int(time.time())
        try:
            await self.conn.execute(
                "INSERT INTO guessyear_achievements (guild_id, user_id, achievement_key, earned_at) VALUES (?, ?, ?, ?)",
                (str(guild_id), str(user_id), key, now),
            )
            await self.conn.commit()
            return True
        except Exception:
            return False
