from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Tuple

import aiosqlite

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
        await self._conn.executescript(sql)
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
