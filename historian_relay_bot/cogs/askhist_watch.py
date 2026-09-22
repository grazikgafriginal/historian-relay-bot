from __future__ import annotations

import asyncio
import logging
import time

import discord
from discord import app_commands
from discord.ext import commands

from historian_relay_bot.utils.checks import is_probable_question
from historian_relay_bot.ui.views import AskHistoriansReminderView, HistorianRequestView, has_role

log = logging.getLogger("historian_relay.askhist_watch")


class AskHistorianWatchCog(commands.Cog):
    """Proactively surfaces /askhist-style help for questions asked as plain chat.

    Watches configured channels (e.g. #history-general) for question-shaped
    messages, waits a configurable delay, and if nothing answered it, offers
    the asker a one-click way to ping the Historians of the House — instead
    of requiring them to already know the /askhist command exists.

    The actual "ping + track a Historian Request" logic lives on the bot
    (see HistorianRelayBot.escalate_to_historians in bot.py) so this cog and
    the moderator context-menu command below both go through one
    implementation.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._reminder_tasks: dict[int, asyncio.Task] = {}
        self._restore_started = False

        self.ask_historians_ctx = app_commands.ContextMenu(
            name="Ask Historians",
            callback=self.ask_historians_context,
        )
        self.bot.tree.add_command(self.ask_historians_ctx)

    async def cog_load(self) -> None:
        if not self._restore_started:
            self._restore_started = True
            asyncio.create_task(self._restore_after_ready())

    async def cog_unload(self) -> None:
        for task in self._reminder_tasks.values():
            task.cancel()
        self.bot.tree.remove_command(self.ask_historians_ctx.name, type=self.ask_historians_ctx.type)

    # --------------------------
    # Helpers
    # --------------------------

    def _watched_channel(self, channel_id: int) -> bool:
        return channel_id in self.bot.cfg.ASKHIST_WATCH_CHANNEL_IDS

    def _is_mod(self, member: discord.Member) -> bool:
        return has_role(member, self.bot.cfg.MOD_ROLE_ID)

    def _is_historian(self, member: discord.Member) -> bool:
        return has_role(member, self.bot.cfg.VERIFIED_HISTORIAN_ROLE_ID)

    def _schedule_reminder(self, watch_id: int, remind_at: int) -> None:
        old = self._reminder_tasks.get(watch_id)
        if old and not old.done():
            old.cancel()
        self._reminder_tasks[watch_id] = asyncio.create_task(self._fire_when_ready(watch_id, remind_at))

    def _cancel_reminder(self, watch_id: int) -> None:
        old = self._reminder_tasks.pop(watch_id, None)
        if old and not old.done():
            old.cancel()

    async def _fire_when_ready(self, watch_id: int, remind_at: int) -> None:
        delay = max(0, remind_at - int(time.time()))
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return

        try:
            row = await self.bot.db.askhist_watch_get_by_id(watch_id)
            if row:
                await self.bot.send_watch_reminder(int(row["guild_id"]), watch_id)
        except Exception:
            log.exception("Failed to send watch reminder for watch #%s", watch_id)
        finally:
            self._reminder_tasks.pop(watch_id, None)

    async def _restore_after_ready(self) -> None:
        await self.bot.wait_until_ready()

        try:
            for row in await self.bot.db.askhist_watch_list_pending():
                self._schedule_reminder(int(row["id"]), int(row["remind_at"]))

            for row in await self.bot.db.askhist_watch_list_reminded():
                await self._reattach_reminder_view(row)
                await asyncio.sleep(0.2)

            for row in await self.bot.db.askhist_watch_list_escalated():
                await self._reattach_request_view(row)
                await asyncio.sleep(0.2)
        except Exception:
            log.exception("Failed to restore Ask Historians watch state")

    async def _reattach_reminder_view(self, row) -> None:
        watch_id = int(row["id"])
        if row["reminder_channel_message_id"]:
            try:
                ch = await self.bot.fetch_channel(int(row["channel_id"]))
                msg = await ch.fetch_message(int(row["reminder_channel_message_id"]))
                await msg.edit(view=AskHistoriansReminderView(self.bot, watch_id))
            except Exception:
                pass
        if row["reminder_dm_message_id"]:
            try:
                user = await self.bot.fetch_user(int(row["author_id"]))
                dm = await user.create_dm()
                msg = await dm.fetch_message(int(row["reminder_dm_message_id"]))
                await msg.edit(view=AskHistoriansReminderView(self.bot, watch_id))
            except Exception:
                pass

    async def _reattach_request_view(self, row) -> None:
        if not row["historian_request_message_id"]:
            return
        try:
            ch = await self.bot.fetch_channel(self.bot.cfg.HISTORIANS_CHANNEL_ID)
            msg = await ch.fetch_message(int(row["historian_request_message_id"]))
            await msg.edit(view=HistorianRequestView(self.bot, int(row["id"])))
        except Exception:
            pass

    # --------------------------
    # Detection + reply tracking
    # --------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not message.guild:
            return
        if not self.bot.cfg.ASKHIST_WATCH_ENABLED:
            return

        if message.reference and message.reference.message_id:
            await self._handle_possible_reply(message)

        if self._watched_channel(message.channel.id) and is_probable_question(
            message.content or "", self.bot.cfg.ASKHIST_WATCH_MIN_WORDS
        ):
            await self._track_new_question(message)

    async def _track_new_question(self, message: discord.Message) -> None:
        now = int(time.time())
        remind_at = now + int(self.bot.cfg.ASKHIST_WATCH_DELAY_SECONDS)
        try:
            watch_id = await self.bot.db.askhist_watch_create(
                guild_id=message.guild.id,
                channel_id=message.channel.id,
                message_id=message.id,
                author_id=message.author.id,
                question_text=message.content,
                created_at=now,
                remind_at=remind_at,
            )
        except Exception:
            log.exception("Failed to record watched question for message %s", message.id)
            return

        if watch_id:
            self._schedule_reminder(watch_id, remind_at)

    async def _handle_possible_reply(self, message: discord.Message) -> None:
        ref_id = message.reference.message_id
        if ref_id is None:
            return

        try:
            found = await self.bot.db.askhist_watch_find_by_ref(message.guild.id, ref_id)
        except Exception:
            return
        if not found:
            return

        row, matched_as = found
        if row["status"] in ("resolved", "cancelled"):
            return

        member = message.author if isinstance(message.author, discord.Member) else None
        watch_id = int(row["id"])

        if member is not None and self._is_historian(member):
            await self.bot.db.historian_response_record(
                guild_id=message.guild.id,
                watch_id=watch_id,
                question_message_id=int(row["message_id"]),
                historian_user_id=member.id,
                response_message_id=message.id,
                after_official_request=bool(row["escalated"]),
            )
            self._cancel_reminder(watch_id)
            await self.bot.resolve_askhist_watch(
                message.guild.id, watch_id,
                resolved_by_user_id=member.id,
                response_message_id=message.id,
                response_channel_id=message.channel.id,
                after_official_request=bool(row["escalated"]),
            )
            return

        # A plain reply from anyone else to the original question ends the
        # timer flow (requirement: "if somebody has already replied to it,
        # the process should end and nothing should be posted").
        if (
            matched_as == "original"
            and row["status"] == "watching"
            and message.author.id != int(row["author_id"])
        ):
            await self.bot.db.askhist_watch_mark_answered(message.guild.id, watch_id)
            self._cancel_reminder(watch_id)

    # --------------------------
    # Moderator manual escalation
    # --------------------------

    async def ask_historians_context(self, interaction: discord.Interaction, message: discord.Message) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("This can only be used in a server.", ephemeral=True)
        if not self._is_mod(interaction.user):
            return await interaction.response.send_message(
                "Only moderators can escalate a message to the Historians.", ephemeral=True
            )
        if message.author.bot:
            return await interaction.response.send_message("Can't escalate a bot message.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        row = await self.bot.db.askhist_watch_get_by_message(interaction.guild_id, message.id)
        if row:
            watch_id = int(row["id"])
            self._cancel_reminder(watch_id)
        else:
            watch_id = await self.bot.db.askhist_watch_create(
                guild_id=interaction.guild_id,
                channel_id=message.channel.id,
                message_id=message.id,
                author_id=message.author.id,
                question_text=message.content or "(no text content)",
                created_at=int(message.created_at.timestamp()),
                remind_at=int(time.time()),
            )

        ok, msg = await self.bot.escalate_to_historians(
            interaction.guild_id,
            watch_id,
            triggered_by_user_id=interaction.user.id,
            is_mod_override=True,
        )
        await interaction.followup.send(msg, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(AskHistorianWatchCog(bot))
