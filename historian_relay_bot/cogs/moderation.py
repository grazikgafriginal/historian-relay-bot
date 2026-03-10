from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

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
        )
        await interaction.response.send_message(text, delete_after=20)

async def setup(bot: commands.Bot):
    await bot.add_cog(ModerationCog(bot))
