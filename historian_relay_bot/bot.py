from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Optional

import discord
from discord.ext import commands

from historian_relay_bot.config import load_config
from historian_relay_bot.db import Database
from historian_relay_bot.ui.views import (
    HistorianView,
    QueueView,
    HistorianRequestView,
)
from historian_relay_bot.utils.formatting import (
    build_forward_embed,
    build_answer_embed,
    build_historian_request_embed,
)
from historian_relay_bot.utils.checks import spam_check, next_utc_midnight_ts

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("historian_relay")
#comit

class _SingleInstanceLock:
    """Best-effort single-instance guard.

    If you accidentally leave an old copy of the bot running and then start a
    new one, Discord will deliver events to both sessions and you'll see
    duplicated bot messages.

    This lock prevents multiple processes on the same machine from starting.
    """

    def __init__(self, name: str = "historian_relay_bot"):
        self.name = name
        self._fp = None

    def acquire_or_exit(self) -> None:
        lock_path = Path(os.getenv("BOT_LOCK_PATH") or (Path.home() / f".{self.name}.lock"))
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        # Keep the file handle open for the lifetime of the process.
        self._fp = open(lock_path, "a+", encoding="utf-8")

        # POSIX (macOS/Linux): flock
        try:
            import fcntl  # type: ignore

            try:
                fcntl.flock(self._fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._fp.seek(0)
                self._fp.truncate(0)
                self._fp.write(f"pid={os.getpid()}\n")
                self._fp.flush()
                return
            except OSError:
                print(
                    "Another instance of the bot is already running on this machine.\n"
                    "Stop the older process (or change BOT_LOCK_PATH) and try again.",
                    file=sys.stderr,
                )
                raise SystemExit(1)
        except ImportError:
            # Windows fallback: best-effort exclusive create.
            # Not as robust as flock, but better than nothing.
            try:
                os.open(str(lock_path) + ".win", os.O_CREAT | os.O_EXCL | os.O_RDWR)
            except FileExistsError:
                print(
                    "Another instance of the bot may already be running on this machine.\n"
                    "Stop the older process and try again.",
                    file=sys.stderr,
                )
                raise SystemExit(1)

class HistorianRelayBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True  # not required for slash/buttons
        intents.guilds = True
        intents.members = True  # needed for on_member_join (referral tracking)
        super().__init__(command_prefix="!", intents=intents)
        self.cfg = load_config()
        self.db = Database(self.cfg.DATABASE_PATH)

    async def setup_hook(self) -> None:
        try:
            log.info("Database path: %s", Path(self.cfg.DATABASE_PATH).expanduser().resolve())
        except Exception:
            log.info("Database path: %s", self.cfg.DATABASE_PATH)
        await self.db.connect()
        await self.db.init_schema("schema.sql")

        # Load cogs
        await self.load_extension("historian_relay_bot.cogs.askhist")
        await self.load_extension("historian_relay_bot.cogs.moderation")
        await self.load_extension("historian_relay_bot.cogs.suggest")

        if self.cfg.ASKHIST_MENTION_ENABLED:
            await self.load_extension("historian_relay_bot.cogs.askhist_mention")

        if self.cfg.TOPIC_OF_DAY_ENABLED:
            await self.load_extension("historian_relay_bot.cogs.topic_of_day")

        # Sync commands (global sync can take time; for a single server consider guild-specific sync)
        await self.tree.sync()

        if self.cfg.GUESSYEAR_ENABLED:
            await self.load_extension("historian_relay_bot.cogs.guessyear")
            await self.load_extension("historian_relay_bot.cogs.duel_tournament")


    async def on_ready(self):
        log.info("Logged in as %s (%s)", self.user, self.user.id)
        await self.restore_views_all_guilds()
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.playing,
                name="!guessyear & /askhist"
            )
        )

    # --------------------------
    # Forwarding + workflows
    # --------------------------

    async def forward_to_historians(self, guild_id: int, qid: int) -> discord.Message:
        q = await self.db.get_question(guild_id, qid)
        if not q:
            raise RuntimeError("Question missing.")

        author = await self.fetch_user(int(q.created_by_user_id))

        origin_jump = ""
        origin_ch = await self.fetch_channel(int(q.origin_channel_id))
        try:
            origin_msg = await origin_ch.fetch_message(int(q.origin_message_id))
            origin_jump = origin_msg.jump_url
        except Exception:
            origin_jump = "https://discord.com"
        claimed_by_text = None
        if q.claimed_by_user_id:
            try:
                cu = await self.fetch_user(int(q.claimed_by_user_id))
                claimed_by_text = cu.mention
            except Exception:
                claimed_by_text = f"`{q.claimed_by_user_id}`"
        embed = build_forward_embed(
            qid=q.id,
            question_text=q.question_text,
            author=author,
            origin_jump_url=origin_jump,
            tag=q.tag,
            era=q.era,
            status=q.status,
            claimed_by_text=claimed_by_text,  # <-- NEW
        )

        hist_ch = await self.fetch_channel(self.cfg.HISTORIANS_CHANNEL_ID)
        view = HistorianView(self, qid=qid)
        msg = await hist_ch.send(embed=embed, view=view)
        return msg

    async def forward_to_queue(self, guild_id: int, qid: int) -> discord.Message:
        if not self.cfg.QUEUE_CHANNEL_ID:
            raise RuntimeError("QUEUE_CHANNEL_ID not set but approval mode enabled.")
        q = await self.db.get_question(guild_id, qid)
        if not q:
            raise RuntimeError("Question missing.")

        author = await self.fetch_user(int(q.created_by_user_id))

        origin_jump = ""
        origin_ch = await self.fetch_channel(int(q.origin_channel_id))
        try:
            origin_msg = await origin_ch.fetch_message(int(q.origin_message_id))
            origin_jump = origin_msg.jump_url
        except Exception:
            origin_jump = "https://discord.com"
        claimed_by_text = None
        if q.claimed_by_user_id:
            try:
                cu = await self.fetch_user(int(q.claimed_by_user_id))
                claimed_by_text = cu.mention
            except Exception:
                claimed_by_text = f"`{q.claimed_by_user_id}`"
        embed = build_forward_embed(
            qid=q.id,
            question_text=q.question_text,
            author=author,
            origin_jump_url=origin_jump,
            tag=q.tag,
            era=q.era,
            status=q.status,
            claimed_by_text=claimed_by_text,  # <-- NEW
        )

        queue_ch = await self.fetch_channel(self.cfg.QUEUE_CHANNEL_ID)
        view = QueueView(self, qid=qid)
        msg = await queue_ch.send(embed=embed, view=view)
        return msg

    async def approve_from_queue(self, guild_id: int, qid: int) -> None:
        q = await self.db.get_question(guild_id, qid)
        if not q or q.status != "queued":
            return

        await self.db.update_status(guild_id, qid, "pending")
        hist_msg = await self.forward_to_historians(guild_id, qid)
        await self.db.set_question_message_refs(guild_id, qid, hist_message_id=hist_msg.id)

    async def deny_from_queue(self, guild_id: int, qid: int, reason: Optional[str]) -> None:
        q = await self.db.get_question(guild_id, qid)
        if not q or q.status != "queued":
            return
        await self.db.update_status(guild_id, qid, "denied")

        await self.notify_asker(guild_id, qid, f"Your question **#{qid}** was denied by moderators." + (f" Reason: {reason}" if reason else ""))

    async def publish_answer(self, interaction: discord.Interaction, *, qid: int, answer_text: str) -> None:
        q = await self.db.get_question(interaction.guild_id, qid)
        if not q:
            return await interaction.followup.send("Question not found.", delete_after=20)

        await self.db.set_answer(interaction.guild_id, qid, answer_text=answer_text, answered_by_user_id=interaction.user.id)

        answer_embed = build_answer_embed(qid=qid, answer_text=answer_text, answered_by=interaction.user)

        posted_url: Optional[str] = None

        # Prefer thread
        if q.origin_thread_id:
            try:
                th = await self.fetch_channel(int(q.origin_thread_id))
                msg = await th.send(embed=answer_embed)
                posted_url = msg.jump_url
            except Exception:
                posted_url = None

        # Fallback origin channel
        if posted_url is None:
            try:
                ch = await self.fetch_channel(int(q.origin_channel_id))
                content = f"<@{q.created_by_user_id}> Answer for **#{qid}**:" if self.cfg.PING_USER_ON_PUBLISH else None
                msg = await ch.send(embed=answer_embed, content=content)
                posted_url = msg.jump_url
            except Exception:
                posted_url = None

        # Refresh forwarded embed (if exists)
        try:
            if q.hist_message_id:
                hist_ch = await self.fetch_channel(self.cfg.HISTORIANS_CHANNEL_ID)
                hist_msg = await hist_ch.fetch_message(int(q.hist_message_id))
                author = await self.fetch_user(int(q.created_by_user_id))

                origin_jump = ""
                try:
                    origin_ch = await self.fetch_channel(int(q.origin_channel_id))
                    origin_msg = await origin_ch.fetch_message(int(q.origin_message_id))
                    origin_jump = origin_msg.jump_url
                except Exception:
                    origin_jump = "https://discord.com"

                new_q = await self.db.get_question(interaction.guild_id, qid)
                embed = build_forward_embed(
                    qid=qid,
                    question_text=new_q.question_text,
                    author=author,
                    origin_jump_url=origin_jump,
                    tag=new_q.tag,
                    era=new_q.era,
                    status=new_q.status,
                )
                if posted_url:
                    embed.add_field(name="Published Answer", value=f"[Jump to answer]({posted_url})", inline=False)
                await hist_msg.edit(embed=embed, view=HistorianView(self, qid))
        except Exception as e:
            log.warning("Failed to refresh forwarded embed for Q#%s: %s", qid, e)

        await self.notify_asker(interaction.guild_id, qid, f"An answer was published for your question **#{qid}**." + (f" {posted_url}" if posted_url else ""))

        await interaction.followup.send(f"Published answer for **#{qid}**.", delete_after=20)
        self.schedule_cleanup(interaction.guild_id, qid, delay_seconds=60)


    async def post_closed_unclear(
        self,
        guild_id: int,
        qid: int,
        *,
        closed_by_id: int | None = None,
        reason: str | None = None,
    ) -> None:
        q = await self.db.get_question(guild_id, qid)
        if not q:
            return

        who = f"<@{closed_by_id}>" if closed_by_id else "A historian"

        default_text = (
            f"⚠️ {who} closed this question as **unclear**.\n"
            "If you'd like to ask again, please include:\n"
            "• timeframe (year/century)\n"
            "• region/place\n"
            "• what you’ve already read/considered\n"
            "• what kind of explanation you want"
        )

        if reason:
            msg_text = (
                f"⚠️ Staff member {who} closed this question.\n"
                f"**Reason:** {reason}\n\n"
                f"*This thread will be closed in **60s**"
            )
        else:
            msg_text = default_text

        # Prefer thread
        if q.origin_thread_id:
            try:
                th = await self.fetch_channel(int(q.origin_thread_id))
                await th.send(content=f"<@{q.created_by_user_id}>\n{msg_text}")
                return
            except Exception:
                pass

        # Fallback origin channel
        try:
            ch = await self.fetch_channel(int(q.origin_channel_id))
            await ch.send(content=f"<@{q.created_by_user_id}>\n{msg_text}")
        except Exception:
            pass


    def schedule_cleanup(self, guild_id: int, qid: int, delay_seconds: int = 60) -> None:
        key = (int(guild_id), int(qid))

        if not hasattr(self, "_cleanup_tasks"):
            self._cleanup_tasks = {}

        existing = self._cleanup_tasks.get(key)
        if existing and not existing.done():
            return  # already scheduled

        async def _run():
            try:
                await asyncio.sleep(delay_seconds)

                q = await self.db.get_question(guild_id, qid)
                if not q:
                    return

                # Only cleanup once resolved
                if q.status not in {"answered", "closed"}:
                    return

                # 1) delete thread first
                if q.origin_thread_id:
                    try:
                        th = await self.fetch_channel(int(q.origin_thread_id))
                        await th.delete()
                    except Exception:
                        pass

                # 2) delete origin message (the message that created the thread)
                try:
                    ch = await self.fetch_channel(int(q.origin_channel_id))
                    msg = await ch.fetch_message(int(q.origin_message_id))
                    await msg.delete()
                except Exception:
                    pass

            finally:
                self._cleanup_tasks.pop(key, None)

        self._cleanup_tasks[key] = asyncio.create_task(_run())


    async def post_needs_context(self, guild_id: int, qid: int) -> None:
        q = await self.db.get_question(guild_id, qid)
        if not q:
            return
        msg_text = (
            "Historians requested more context:\n"
            "• timeframe (year/century)\n"
            "• region/place\n"
            "• what you’ve already read/considered\n"
            "• what kind of explanation you want\n"
        )
        # Prefer thread
        if q.origin_thread_id:
            try:
                th = await self.fetch_channel(int(q.origin_thread_id))
                await th.send(content=f"<@{q.created_by_user_id}>\n{msg_text}")
                return
            except Exception:
                pass
        # Fallback origin channel
        try:
            ch = await self.fetch_channel(int(q.origin_channel_id))
            await ch.send(content=f"<@{q.created_by_user_id}> {msg_text}")
        except Exception:
            pass

    async def notify_asker(self, guild_id: int, qid: int, text: str) -> None:
        q = await self.db.get_question(guild_id, qid)
        if not q:
            return
        # Prefer thread ping if configured and thread exists
        if self.cfg.PING_USER_ON_PUBLISH and q.origin_thread_id:
            try:
                th = await self.fetch_channel(int(q.origin_thread_id))
                await th.send(content=f"<@{q.created_by_user_id}> {text}")
                return
            except Exception:
                pass
        # Try DM
        try:
            user = await self.fetch_user(int(q.created_by_user_id))
            await user.send(text)
        except Exception:
            pass

    # --------------------------
    # Ask Historians forwarding
    #
    # These methods are the single implementation responsible for pinging and
    # tracking Historian Requests. They're called from both triggers:
    # a user @-mentioning the bot (cogs/askhist_mention.py) and a moderator's
    # "Ask Historians" context-menu command (also cogs/askhist_mention.py).
    # There is no passive channel-watching or unanswered-question timer —
    # forwarding happens immediately on an explicit trigger.
    # --------------------------

    async def escalate_to_historians(
        self,
        guild_id: int,
        forward_id: int,
        *,
        triggered_by_user_id: int,
        is_mod_override: bool = False,
    ) -> tuple[bool, str]:
        """Create the Historian Request and ping the Historian role.

        This is the single function responsible for pinging + tracking a
        Historian Request — both the @mention trigger and the moderator
        context-menu command call this.
        """
        row = await self.db.askhist_forward_get(guild_id, forward_id)
        if not row:
            return False, "This question could not be found."

        if row["status"] in ("escalated", "resolved", "cancelled"):
            return False, "This question has already been sent to the Historians of the House."

        now_ts = int(discord.utils.utcnow().timestamp())
        daily_count = 0
        daily_reset_at = next_utc_midnight_ts(now_ts)

        if not is_mod_override:
            cd = await self.db.historian_ping_cooldown_get(guild_id, triggered_by_user_id)
            last_ping_at = int(cd["last_ping_at"]) if cd else None
            daily_count = int(cd["daily_count"]) if cd else 0
            daily_reset_at = int(cd["daily_reset_at"]) if cd else next_utc_midnight_ts(now_ts)

            spam_res = spam_check(
                now_ts=now_ts,
                last_asked_at=last_ping_at,
                daily_count=daily_count,
                daily_reset_at=daily_reset_at,
                cooldown_minutes=self.cfg.ASKHIST_MENTION_COOLDOWN_MINUTES,
                max_per_day=self.cfg.ASKHIST_MENTION_MAX_PER_DAY,
            )
            if not spam_res.ok:
                reason = spam_res.reason.replace("submitting another question", "requesting the Historians again")
                return False, reason

        ok = await self.db.askhist_forward_try_escalate(guild_id, forward_id, triggered_by_user_id)
        if not ok:
            return False, "This question has already been sent to the Historians of the House."

        row = await self.db.askhist_forward_get(guild_id, forward_id)  # refreshed after escalation

        origin_jump = f"https://discord.com/channels/{guild_id}/{row['channel_id']}/{row['message_id']}"
        try:
            origin_ch = await self.fetch_channel(int(row["channel_id"]))
            origin_msg = await origin_ch.fetch_message(int(row["message_id"]))
            origin_jump = origin_msg.jump_url
        except Exception:
            pass

        asker = None
        try:
            asker = await self.fetch_user(int(row["author_id"]))
        except Exception:
            pass

        embed = build_historian_request_embed(
            question_text=str(row["question_text"]),
            asker=asker,
            jump_url=origin_jump,
        )

        try:
            hist_ch = await self.fetch_channel(self.cfg.HISTORIANS_CHANNEL_ID)
            content = (
                f"<@&{self.cfg.VERIFIED_HISTORIAN_ROLE_ID}>\n"
                "If this falls within your area of knowledge, please take a look."
            )
            req_view = HistorianRequestView(self, forward_id)
            req_msg = await hist_ch.send(
                content=content,
                embed=embed,
                view=req_view,
                allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False),
            )
            await self.db.askhist_forward_set_request_message(guild_id, forward_id, req_msg.id)
        except Exception as e:
            log.warning("Failed to post Historian Request for forward #%s: %s", forward_id, e)
            return True, "Historians could not be notified automatically — please contact a moderator."

        if not is_mod_override:
            if now_ts >= daily_reset_at:
                daily_count = 0
                daily_reset_at = next_utc_midnight_ts(now_ts)
            daily_count += 1
            await self.db.historian_ping_cooldown_upsert(
                guild_id, triggered_by_user_id,
                last_ping_at=now_ts, daily_count=daily_count, daily_reset_at=daily_reset_at,
            )

        return True, "📚 Historians of the House have been notified about this question!"

    async def resolve_askhist_forward(
        self,
        guild_id: int,
        forward_id: int,
        *,
        resolved_by_user_id: int,
        response_message_id: Optional[int],
        after_official_request: bool,
        response_channel_id: Optional[int] = None,
    ) -> bool:
        """Mark a forwarded question resolved and record it for KPI tracking."""
        ok = await self.db.askhist_forward_try_resolve(guild_id, forward_id, resolved_by_user_id)
        if not ok:
            return False

        row = await self.db.askhist_forward_get(guild_id, forward_id)
        if not row:
            return True

        if row["historian_request_message_id"]:
            try:
                ch = await self.fetch_channel(self.cfg.HISTORIANS_CHANNEL_ID)
                msg = await ch.fetch_message(int(row["historian_request_message_id"]))

                try:
                    resolver = await self.fetch_user(resolved_by_user_id)
                    resolver_text = resolver.mention
                except Exception:
                    resolver_text = f"<@{resolved_by_user_id}>"

                origin_jump = f"https://discord.com/channels/{guild_id}/{row['channel_id']}/{row['message_id']}"
                try:
                    origin_ch = await self.fetch_channel(int(row["channel_id"]))
                    origin_msg = await origin_ch.fetch_message(int(row["message_id"]))
                    origin_jump = origin_msg.jump_url
                except Exception:
                    pass

                asker = None
                try:
                    asker = await self.fetch_user(int(row["author_id"]))
                except Exception:
                    pass

                embed = build_historian_request_embed(
                    question_text=str(row["question_text"]),
                    asker=asker,
                    jump_url=origin_jump,
                    resolved_by_text=resolver_text,
                )
                if response_message_id and response_channel_id:
                    response_jump = f"https://discord.com/channels/{guild_id}/{response_channel_id}/{response_message_id}"
                    embed.add_field(name="Response", value=f"[Jump to response]({response_jump})", inline=False)
                await msg.edit(embed=embed, view=None)
            except Exception as e:
                log.warning("Failed to refresh Historian Request for forward #%s: %s", forward_id, e)

        return True

    # --------------------------
    # Restart recovery
    # --------------------------

    async def restore_views_all_guilds(self) -> None:
        for guild in list(self.guilds):
            await self.restore_views_for_guild(guild.id)

    async def restore_views_for_guild(self, guild_id: int) -> None:
        data = await self.db.list_messages_to_restore(guild_id)

        # Historians messages
        for q in data["hist"]:
            try:
                ch = await self.fetch_channel(self.cfg.HISTORIANS_CHANNEL_ID)
                msg = await ch.fetch_message(int(q.hist_message_id))
                await msg.edit(view=HistorianView(self, q.id))
                await asyncio.sleep(0.2)
            except Exception:
                continue

        # Queue messages
        if self.cfg.QUEUE_CHANNEL_ID:
            for q in data["queue"]:
                try:
                    ch = await self.fetch_channel(self.cfg.QUEUE_CHANNEL_ID)
                    msg = await ch.fetch_message(int(q.queue_message_id))
                    await msg.edit(view=QueueView(self, q.id))
                    await asyncio.sleep(0.2)
                except Exception:
                    continue

async def main():
    _SingleInstanceLock().acquire_or_exit()
    
    bot = HistorianRelayBot()  # <-- this was missing
    
    delay = 5
    max_delay = 300

    while True:
        try:
            await bot.start(bot.cfg.DISCORD_TOKEN)
            break
        except discord.errors.HTTPException as e:
            if e.status == 429:
                retry_after = e.response.headers.get("Retry-After")
                wait = float(retry_after) if retry_after else delay
                print(f"Rate limited on login. Waiting {wait:.1f}s before retrying...")
                await asyncio.sleep(wait)
                delay = min(delay * 2, max_delay)
            else:
                raise

if __name__ == "__main__":
    asyncio.run(main())
