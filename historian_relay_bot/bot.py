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
from historian_relay_bot.ui.views import HistorianView, QueueView
from historian_relay_bot.utils.formatting import build_forward_embed, build_answer_embed

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
        intents.members = False  # role checks
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
    if config.GUESSYEAR_ENABLED:
        await self.load_extension("historian_relay_bot.cogs.guessyear")
        await self.load_extension("historian_relay_bot.cogs.duel_tournament")

        # Sync commands (global sync can take time; for a single server consider guild-specific sync)
        await self.tree.sync()
        if self.cfg.GUESSYEAR_ENABLED:
            await self.load_extension("historian_relay_bot.cogs.guessyear")


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
    # Prevent running two copies of the bot at the same time on the same machine.
    _SingleInstanceLock().acquire_or_exit()
    bot = HistorianRelayBot()
    async with bot:
        await bot.start(bot.cfg.DISCORD_TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
