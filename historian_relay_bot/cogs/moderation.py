from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Optional, Union

import discord
from discord import app_commands
from discord.ext import commands

from historian_relay_bot.utils.formatting import shorten_title

log = logging.getLogger("historian_relay.moderation")

MIN_PURGE_KEYWORD_LENGTH = 2
DEFAULT_PURGE_SCAN_LIMIT = 500
MAX_PURGE_SCAN_LIMIT = 2000
PURGE_PREVIEW_SAMPLES = 5
PURGE_CONFIRM_TIMEOUT_SECONDS = 120
BULK_DELETE_MAX_AGE = timedelta(days=14)  # Discord can only bulk-delete messages younger than this.
BULK_DELETE_CHUNK_SIZE = 100  # Discord's bulk-delete endpoint caps at 100 messages per call.


class PurgeConfirmView(discord.ui.View):
    """Preview-then-confirm gate in front of a destructive, irreversible bulk delete.

    Sent as an ephemeral followup, so only the moderator who ran the command
    can ever see or press these buttons.
    """

    def __init__(
        self,
        cog: "ModerationCog",
        *,
        requested_by_id: int,
        channel: Union[discord.TextChannel, discord.Thread],
        keyword: str,
        matches: list[discord.Message],
    ):
        super().__init__(timeout=PURGE_CONFIRM_TIMEOUT_SECONDS)
        self.cog = cog
        self.requested_by_id = requested_by_id
        self.channel = channel
        self.keyword = keyword
        self.matches = matches
        self.message: Optional[discord.Message] = None
        self._done = False

    async def on_timeout(self) -> None:
        if self._done:
            return
        self._done = True
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(content="⌛ Purge confirmation timed out — no messages were deleted.", embed=None, view=self)
            except Exception:
                pass

    async def _guard(self, interaction: discord.Interaction) -> bool:
        # This message is ephemeral, so only requested_by_id can ever see it —
        # this check is primarily a safeguard against a stale/duplicate interaction.
        if interaction.user.id != self.requested_by_id:
            await interaction.response.send_message("Only the moderator who ran this can confirm it.", ephemeral=True)
            return False
        if self._done:
            await interaction.response.send_message("This purge has already been handled.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="✅ Confirm Delete", style=discord.ButtonStyle.danger)
    async def confirm_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        self._done = True
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="🗑️ Deleting…", embed=None, view=self)

        deleted, failed = await self.cog._delete_messages(self.channel, self.matches)

        result_text = f"✅ Deleted **{deleted}** message(s) containing `{self.keyword}` in {self.channel.mention}."
        if failed:
            result_text += f"\n⚠️ {failed} message(s) could not be deleted (already removed or missing permissions)."
        try:
            await self.message.edit(content=result_text, view=self)
        except Exception:
            pass

        await self.cog._log_purge(
            moderator=interaction.user,
            channel=self.channel,
            keyword=self.keyword,
            deleted=deleted,
            failed=failed,
        )

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        self._done = True
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="❌ Purge cancelled — no messages were deleted.", embed=None, view=self)


class ModerationCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _is_mod(self, member: discord.Member) -> bool:
        return any(r.id == self.bot.cfg.MOD_ROLE_ID for r in member.roles)

    @app_commands.command(name="askhist_blacklist", description="Blacklist a user from /askhist.")
    @app_commands.describe(user="User to blacklist", reason="Reason (optional)")
    async def blacklist(self, interaction: discord.Interaction, user: discord.User, reason: str | None = None):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return
        if not self._is_mod(interaction.user):
            return await interaction.response.send_message("Mods only.", delete_after=20)

        await self.bot.db.set_blacklist(interaction.guild_id, user.id, reason, interaction.user.id)
        await interaction.response.send_message(f"Blacklisted {user.mention}.", delete_after=20)

    @app_commands.command(name="askhist_unblacklist", description="Remove a user from the /askhist blacklist.")
    @app_commands.describe(user="User to unblacklist")
    async def unblacklist(self, interaction: discord.Interaction, user: discord.User):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return
        if not self._is_mod(interaction.user):
            return await interaction.response.send_message("Mods only.", delete_after=20)

        await self.bot.db.remove_blacklist(interaction.guild_id, user.id)
        await interaction.response.send_message(f"Unblacklisted {user.mention}.", delete_after=20)

    @app_commands.command(name="askhist_config", description="Show current Historians of the House config.")
    async def config_cmd(self, interaction: discord.Interaction):
        cfg = self.bot.cfg
        text = (
            f"**Approval mode:** {cfg.APPROVAL_MODE}\n"
            f"**Threads enabled:** {cfg.THREADS_ENABLED}\n"
            f"**Min length:** {cfg.MIN_QUESTION_LENGTH}\n"
            f"**Cooldown:** {cfg.COOLDOWN_MINUTES} min\n"
            f"**Daily cap:** {cfg.MAX_PER_DAY}\n"
            f"**Account age:** {cfg.REQUIRE_ACCOUNT_AGE_DAYS} days\n"
            f"**Publish w/o claim:** {cfg.ALLOW_PUBLISH_WITHOUT_CLAIM}\n"
            f"**Submission channels:** {', '.join(str(x) for x in cfg.SUBMISSION_CHANNEL_IDS)}\n"
            f"**Historians channel:** {cfg.HISTORIANS_CHANNEL_ID}\n"
            f"**Queue channel:** {cfg.QUEUE_CHANNEL_ID}\n"
            f"\n**Proactive Ask Historians:**\n"
            f"**Enabled:** {cfg.ASKHIST_WATCH_ENABLED}\n"
            f"**Watched channels:** {', '.join(str(x) for x in cfg.ASKHIST_WATCH_CHANNEL_IDS) or '—'}\n"
            f"**Unanswered delay:** {cfg.ASKHIST_WATCH_DELAY_SECONDS}s\n"
            f"**Ping cooldown:** {cfg.ASKHIST_WATCH_PING_COOLDOWN_MINUTES} min\n"
            f"**Ping daily cap:** {cfg.ASKHIST_WATCH_PING_MAX_PER_DAY}\n"
            f"\n**Topic of the Day:**\n"
            f"**Enabled:** {cfg.TOPIC_OF_DAY_ENABLED}\n"
            f"**Channel:** {cfg.TOPIC_OF_DAY_CHANNEL_ID or '—'}\n"
            f"**Post hour (UTC):** {cfg.TOPIC_OF_DAY_HOUR_UTC}\n"
        )
        await interaction.response.send_message(text, delete_after=20)

    @app_commands.command(name="purge_keyword", description="Preview and delete recent messages containing a keyword.")
    @app_commands.describe(
        keyword="Text to search for (case-insensitive substring match by default)",
        channel="Channel or thread to search (defaults to the current channel)",
        limit=f"How many recent messages to scan (default {DEFAULT_PURGE_SCAN_LIMIT}, max {MAX_PURGE_SCAN_LIMIT})",
        case_sensitive="Match exact case instead of case-insensitive (default off)",
    )
    async def purge_keyword(
        self,
        interaction: discord.Interaction,
        keyword: str,
        channel: Optional[Union[discord.TextChannel, discord.Thread]] = None,
        limit: Optional[int] = None,
        case_sensitive: bool = False,
    ):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("Server only.", ephemeral=True)
        if not self._is_mod(interaction.user):
            return await interaction.response.send_message("Mods only.", ephemeral=True)

        keyword = keyword.strip()
        if len(keyword) < MIN_PURGE_KEYWORD_LENGTH:
            return await interaction.response.send_message(
                f"Keyword must be at least {MIN_PURGE_KEYWORD_LENGTH} characters — "
                "anything shorter risks matching unrelated messages.",
                ephemeral=True,
            )

        target = channel or interaction.channel
        if not isinstance(target, (discord.TextChannel, discord.Thread)):
            return await interaction.response.send_message("Pick a text channel or thread.", ephemeral=True)

        scan_limit = max(1, min(int(limit) if limit else DEFAULT_PURGE_SCAN_LIMIT, MAX_PURGE_SCAN_LIMIT))

        await interaction.response.defer(ephemeral=True)

        needle = keyword if case_sensitive else keyword.lower()
        matches: list[discord.Message] = []
        try:
            async for msg in target.history(limit=scan_limit):
                content = msg.content if case_sensitive else msg.content.lower()
                if needle in content:
                    matches.append(msg)
        except discord.Forbidden:
            return await interaction.followup.send(
                "I don't have permission to read message history in that channel.", ephemeral=True
            )

        if not matches:
            return await interaction.followup.send(
                f"No messages containing `{keyword}` found in the last {scan_limit} messages of {target.mention}.",
                ephemeral=True,
            )

        preview_lines = [
            f"• {m.author.mention}: {shorten_title(m.content, 80)}" for m in matches[:PURGE_PREVIEW_SAMPLES]
        ]
        if len(matches) > PURGE_PREVIEW_SAMPLES:
            preview_lines.append(f"…and {len(matches) - PURGE_PREVIEW_SAMPLES} more.")

        embed = discord.Embed(
            title="⚠️ Confirm Purge",
            description=(
                f"Found **{len(matches)}** message(s) containing `{keyword}` "
                f"in the last {scan_limit} messages of {target.mention}."
            ),
            color=discord.Color.orange(),
        )
        embed.add_field(name="Preview", value="\n".join(preview_lines) or "—", inline=False)
        embed.set_footer(text="This cannot be undone. Confirm to delete, or cancel.")

        view = PurgeConfirmView(
            self,
            requested_by_id=interaction.user.id,
            channel=target,
            keyword=keyword,
            matches=matches,
        )
        msg = await interaction.followup.send(embed=embed, view=view, ephemeral=True, wait=True)
        view.message = msg

    async def _delete_messages(
        self, channel: Union[discord.TextChannel, discord.Thread], messages: list[discord.Message]
    ) -> tuple[int, int]:
        """Bulk-delete where possible (<14 days old), falling back to individual deletes.

        Returns (deleted_count, failed_count).
        """
        now = discord.utils.utcnow()
        young = [m for m in messages if now - m.created_at < BULK_DELETE_MAX_AGE]
        old = [m for m in messages if now - m.created_at >= BULK_DELETE_MAX_AGE]

        deleted = 0
        failed = 0

        for i in range(0, len(young), BULK_DELETE_CHUNK_SIZE):
            chunk = young[i : i + BULK_DELETE_CHUNK_SIZE]
            try:
                await channel.delete_messages(chunk)
                deleted += len(chunk)
            except discord.HTTPException:
                # One of the messages in this chunk may already be gone — fall back per-message.
                for m in chunk:
                    try:
                        await m.delete()
                        deleted += 1
                    except discord.HTTPException:
                        failed += 1
                    await asyncio.sleep(0.3)

        for m in old:
            try:
                await m.delete()
                deleted += 1
            except discord.HTTPException:
                failed += 1
            await asyncio.sleep(0.3)

        return deleted, failed

    async def _log_purge(
        self,
        *,
        moderator: discord.abc.User,
        channel: Union[discord.TextChannel, discord.Thread],
        keyword: str,
        deleted: int,
        failed: int,
    ) -> None:
        log_channel_id = self.bot.cfg.LOG_CHANNEL_ID
        if not log_channel_id:
            return
        try:
            log_ch = await self.bot.fetch_channel(log_channel_id)
            embed = discord.Embed(
                title="🗑️ Keyword Purge",
                description=f"**Moderator:** {moderator.mention}\n**Channel:** {channel.mention}\n**Keyword:** `{keyword}`",
                color=discord.Color.red(),
                timestamp=discord.utils.utcnow(),
            )
            embed.add_field(name="Deleted", value=str(deleted), inline=True)
            if failed:
                embed.add_field(name="Failed", value=str(failed), inline=True)
            await log_ch.send(embed=embed)
        except Exception as e:
            log.warning("Failed to post purge audit log: %s", e)

async def setup(bot: commands.Bot):
    await bot.add_cog(ModerationCog(bot))
