from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

from historian_relay_bot.ui.views import HistorianRequestView, has_role

log = logging.getLogger("historian_relay.askhist_mention")


class AskHistorianMentionCog(commands.Cog):
    """Forwards a message to the Historians of the House when the bot is @-mentioned.

    Replaces the old passive "watch a channel, wait, then nag" flow: an
    @mention is itself the explicit signal that a question needs a
    Historian's attention, so the message is forwarded immediately — no
    timer, no in-channel reminder, no DM. Moderators can also forward any
    message via the "Ask Historians" message context-menu action, independent
    of mentions.

    Both triggers call HistorianRelayBot.escalate_to_historians() (bot.py),
    the single implementation responsible for pinging + tracking a Historian
    Request.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
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
        self.bot.tree.remove_command(self.ask_historians_ctx.name, type=self.ask_historians_ctx.type)

    def _is_mod(self, member: discord.Member) -> bool:
        return has_role(member, self.bot.cfg.MOD_ROLE_ID)

    def _is_historian(self, member: discord.Member) -> bool:
        return has_role(member, self.bot.cfg.VERIFIED_HISTORIAN_ROLE_ID)

    async def _restore_after_ready(self) -> None:
        """Reattach "Mark Resolved" buttons on any Historian Requests still open after a restart."""
        await self.bot.wait_until_ready()
        try:
            for row in await self.bot.db.askhist_forward_list_escalated():
                if not row["historian_request_message_id"]:
                    continue
                try:
                    ch = await self.bot.fetch_channel(self.bot.cfg.HISTORIANS_CHANNEL_ID)
                    msg = await ch.fetch_message(int(row["historian_request_message_id"]))
                    await msg.edit(view=HistorianRequestView(self.bot, int(row["id"])))
                except Exception:
                    continue
        except Exception:
            log.exception("Failed to restore Historian Request views")

    # --------------------------
    # Detection: @mention the bot + reply-based resolution tracking
    # --------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not message.guild:
            return
        if not self.bot.cfg.ASKHIST_MENTION_ENABLED:
            return

        # Reply-based resolution tracking for already-escalated questions —
        # independent of whether this message also mentions the bot.
        if message.reference and message.reference.message_id:
            await self._handle_possible_reply(message)

        if self.bot.user in message.mentions:
            await self._forward_on_mention(message)

    def _strip_mention(self, content: str) -> str:
        for pattern in (f"<@{self.bot.user.id}>", f"<@!{self.bot.user.id}>"):
            content = content.replace(pattern, "")
        return content.strip()

    async def _resolve_own_original(self, message: discord.Message) -> discord.Message | None:
        """If `message` is a reply to the author's own earlier message, return it.

        Lets someone ask a question, then later just reply "@bot" to it (no
        need to retype the question) once nobody's answered — instead of
        forwarding the reply itself (which may have no real question text),
        the original message is what gets forwarded. Returns None if this
        isn't a reply, the original can't be fetched, or it belongs to
        someone else (forwarding someone else's message without their own
        words is left to the moderator context-menu action).
        """
        ref = message.reference
        if not ref or not ref.message_id:
            return None

        original = ref.resolved
        if isinstance(original, discord.DeletedReferencedMessage):
            return None
        if original is None:
            try:
                original = await message.channel.fetch_message(ref.message_id)
            except Exception:
                return None

        if original.author.id != message.author.id:
            return None
        return original

    async def _forward_on_mention(self, message: discord.Message) -> None:
        reply_extra = self._strip_mention(message.content or "")

        anchor = message
        question_text = reply_extra

        original = await self._resolve_own_original(message)
        if original is not None:
            original_text = (original.content or "").strip()
            if original_text:
                anchor = original
                question_text = (
                    f"{original_text}\n\n(Additional context from asker: {reply_extra})"
                    if reply_extra
                    else original_text
                )

        if not question_text:
            try:
                await message.reply(
                    "Mention me together with your question (or reply to your own question and mention "
                    "me there) and I'll forward it to the Historians of the House.",
                    mention_author=True,
                )
            except Exception:
                pass
            return

        try:
            forward_id = await self.bot.db.askhist_forward_create(
                guild_id=anchor.guild.id,
                channel_id=anchor.channel.id,
                message_id=anchor.id,
                author_id=anchor.author.id,
                question_text=question_text,
                created_at=int(anchor.created_at.timestamp()),
            )
        except Exception:
            log.exception("Failed to record forwarded question for message %s", anchor.id)
            return

        if not forward_id:
            return

        ok, reply_text = await self.bot.escalate_to_historians(
            message.guild.id,
            forward_id,
            triggered_by_user_id=message.author.id,
            is_mod_override=False,
        )
        try:
            await message.reply(reply_text, mention_author=False)
        except Exception:
            pass

    async def _handle_possible_reply(self, message: discord.Message) -> None:
        ref_id = message.reference.message_id
        if ref_id is None:
            return

        try:
            found = await self.bot.db.askhist_forward_find_by_ref(message.guild.id, ref_id)
        except Exception:
            return
        if not found:
            return

        row, _matched_as = found
        if row["status"] in ("resolved", "cancelled"):
            return

        member = message.author if isinstance(message.author, discord.Member) else None
        if member is None or not self._is_historian(member):
            return

        forward_id = int(row["id"])
        await self.bot.db.historian_response_record(
            guild_id=message.guild.id,
            watch_id=forward_id,
            question_message_id=int(row["message_id"]),
            historian_user_id=member.id,
            response_message_id=message.id,
            after_official_request=bool(row["escalated"]),
        )
        await self.bot.resolve_askhist_forward(
            message.guild.id,
            forward_id,
            resolved_by_user_id=member.id,
            response_message_id=message.id,
            response_channel_id=message.channel.id,
            after_official_request=bool(row["escalated"]),
        )

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

        row = await self.bot.db.askhist_forward_get_by_message(interaction.guild_id, message.id)
        if row:
            forward_id = int(row["id"])
        else:
            forward_id = await self.bot.db.askhist_forward_create(
                guild_id=interaction.guild_id,
                channel_id=message.channel.id,
                message_id=message.id,
                author_id=message.author.id,
                question_text=message.content or "(no text content)",
                created_at=int(message.created_at.timestamp()),
            )

        ok, reply_text = await self.bot.escalate_to_historians(
            interaction.guild_id,
            forward_id,
            triggered_by_user_id=interaction.user.id,
            is_mod_override=True,
        )
        await interaction.followup.send(reply_text, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(AskHistorianMentionCog(bot))
