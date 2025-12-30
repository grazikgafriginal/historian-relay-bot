from __future__ import annotations

import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from historian_relay_bot.utils.checks import (
    quality_check,
    spam_check,
    account_age_check,
    next_utc_midnight_ts,
)
from historian_relay_bot.utils.formatting import build_origin_embed, shorten_title

log = logging.getLogger("historian_relay.askhist")

TAG_CHOICES = [
    app_commands.Choice(name="Politics", value="politics"),
    app_commands.Choice(name="Military", value="military"),
    app_commands.Choice(name="Culture", value="culture"),
    app_commands.Choice(name="Economy", value="economy"),
    app_commands.Choice(name="Religion", value="religion"),
]

ERA_CHOICES = [
    app_commands.Choice(name="Ancient", value="ancient"),
    app_commands.Choice(name="Medieval", value="medieval"),
    app_commands.Choice(name="Early Modern", value="early_modern"),
    app_commands.Choice(name="Modern", value="modern"),
]

class AskHistCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _allowed_channel(self, channel_id: int) -> bool:
        return channel_id in self.bot.cfg.SUBMISSION_CHANNEL_IDS

    @app_commands.command(name="askhist", description="Submit a history question for verified historians.")
    @app_commands.describe(question="Your question (include time/place/context).", tag="Optional topic tag", era="Optional era")
    @app_commands.choices(tag=TAG_CHOICES, era=ERA_CHOICES)
    async def askhist(
        self,
        interaction: discord.Interaction,
        question: str,
        tag: Optional[app_commands.Choice[str]] = None,
        era: Optional[app_commands.Choice[str]] = None,
    ):
        if not interaction.guild:
            return await interaction.response.send_message("This command can only be used in a server.", delete_after=20)

        if not self._allowed_channel(interaction.channel_id):
            return await interaction.response.send_message("Please use this command in the designated history question channels.", delete_after=20)

        # blacklist
        bl_reason = await self.bot.db.is_blacklisted(interaction.guild_id, interaction.user.id)
        if bl_reason is not None:
            msg = "You are not allowed to submit questions at this time."
            if bl_reason:
                msg += f" Reason: **{bl_reason}**"
            return await interaction.response.send_message(msg, delete_after=20)

        # account age (optional)
        age_res = account_age_check(interaction.user.created_at, self.bot.cfg.REQUIRE_ACCOUNT_AGE_DAYS)
        if not age_res.ok:
            return await interaction.response.send_message(age_res.reason, delete_after=20)

        # cooldown + daily cap
        now_ts = int(discord.utils.utcnow().timestamp())

        cd = await self.bot.db.get_cooldown(interaction.guild_id, interaction.user.id)
        last_asked_at = int(cd["last_asked_at"]) if cd else None
        daily_count = int(cd["daily_count"]) if cd else 0
        daily_reset_at = int(cd["daily_reset_at"]) if cd else next_utc_midnight_ts(now_ts)

        spam_res = spam_check(
            now_ts=now_ts,
            last_asked_at=last_asked_at,
            daily_count=daily_count,
            daily_reset_at=daily_reset_at,
            cooldown_minutes=self.bot.cfg.COOLDOWN_MINUTES,
            max_per_day=self.bot.cfg.MAX_PER_DAY,
        )
        if not spam_res.ok:
            return await interaction.response.send_message(spam_res.reason, delete_after=20)

        # quality checks
        tag_val = tag.value if tag else None
        era_val = era.value if era else None
        qual = quality_check(
            question,
            min_len=self.bot.cfg.MIN_QUESTION_LENGTH,
            tag=tag_val,
            era=era_val,
            region_keywords=self.bot.cfg.REGION_KEYWORDS,
        )
        if not qual.ok:
            return await interaction.response.send_message(qual.reason, delete_after=20)

        await interaction.response.defer(thinking=True, ephemeral=True)

        # Post origin message in channel (stable jump URL + thread anchor)
        origin_embed = build_origin_embed(
            qid=0,  # placeholder until DB assigns
            question_text=question.strip(),
            tag=tag_val,
            era=era_val,
            asker=interaction.user,
        )
        origin_msg = await interaction.channel.send(embed=origin_embed)

        # Create DB row now that we have origin refs
        status = "queued" if self.bot.cfg.APPROVAL_MODE else "pending"
        qid = await self.bot.db.create_question(
            guild_id=interaction.guild_id,
            created_by_user_id=interaction.user.id,
            question_text=question.strip(),
            tag=tag_val,
            era=era_val,
            status=status,
            origin_channel_id=origin_msg.channel.id,
            origin_message_id=origin_msg.id,
            origin_thread_id=None,
        )

        # Update origin embed with real ID
        origin_embed = build_origin_embed(
            qid=qid,
            question_text=question.strip(),
            tag=tag_val,
            era=era_val,
            asker=interaction.user,
        )
        await origin_msg.edit(embed=origin_embed)

        thread = None
        if self.bot.cfg.THREADS_ENABLED and isinstance(origin_msg.channel, discord.TextChannel):
            try:
                thread_name = f"Q#{qid} — {shorten_title(question)}"
                thread = await origin_msg.create_thread(name=thread_name, auto_archive_duration=1440)
                await self.bot.db.set_question_message_refs(interaction.guild_id, qid, origin_thread_id=thread.id)
                await thread.send(f"{interaction.user.mention} Your question has been forwarded. Historians may answer here.")
            except Exception as e:
                log.warning("Thread creation failed for Q#%s: %s", qid, e)

        # Forward to historians or queue
        if self.bot.cfg.APPROVAL_MODE:
            queue_msg = await self.bot.forward_to_queue(interaction.guild_id, qid)
            await self.bot.db.set_question_message_refs(interaction.guild_id, qid, queue_message_id=queue_msg.id)
            dest = "✅ Sent to queue"
        else:
            hist_msg = await self.bot.forward_to_historians(interaction.guild_id, qid)
            await self.bot.db.set_question_message_refs(interaction.guild_id, qid, hist_message_id=hist_msg.id)
            dest = "✅ Forwarded"

        # Update cooldown row
        if now_ts >= daily_reset_at:
            daily_count = 0
            daily_reset_at = next_utc_midnight_ts(now_ts)
        daily_count += 1
        await self.bot.db.upsert_cooldown(
            interaction.guild_id,
            interaction.user.id,
            last_asked_at=now_ts,
            daily_count=daily_count,
            daily_reset_at=daily_reset_at,
        )

        link = thread.jump_url if thread else origin_msg.jump_url
        msg = await interaction.followup.send(
            f"{dest}\nTracking ID: **#{qid}**\nLink: {link}",
            wait=True
        )
        await msg.delete(delay=20)


    @app_commands.command(name="askhist_status", description="Check status for a question.")
    @app_commands.describe(id="Tracking ID (number)")
    async def askhist_status(self, interaction: discord.Interaction, id: int):
        if not interaction.guild:
            return await interaction.response.send_message("Server only.", delete_after=20)

        q = await self.bot.db.get_question(interaction.guild_id, id)
        if not q:
            return await interaction.response.send_message("No question found with that ID.", delete_after=20)

        links = []
        links.append(f"Origin: <#{q.origin_channel_id}> / `{q.origin_message_id}`")
        if q.origin_thread_id:
            links.append(f"Thread: <#{q.origin_thread_id}>")
        if q.hist_message_id:
            links.append(f"Hist msg id: `{q.hist_message_id}`")
        if q.queue_message_id:
            links.append(f"Queue msg id: `{q.queue_message_id}`")

        await interaction.response.send_message(
            f"Question **#{q.id}** status: **{q.status}**\n" + "\n".join(links),
            delete_after=20
        )

    @app_commands.command(name="askhist_cancel", description="Cancel your question (only if pending/queued).")
    @app_commands.describe(id="Tracking ID (number)")
    async def askhist_cancel(self, interaction: discord.Interaction, id: int):
        if not interaction.guild:
            return await interaction.response.send_message("Server only.", delete_after=20)

        q = await self.bot.db.get_question(interaction.guild_id, id)
        if not q:
            return await interaction.response.send_message("No question found with that ID.", delete_after=20)

        if int(q.created_by_user_id) != interaction.user.id:
            return await interaction.response.send_message("Only the original asker can cancel.", delete_after=20)

        if q.status not in ("pending", "queued"):
            return await interaction.response.send_message(f"Cannot cancel in status: **{q.status}**", delete_after=20)

        await self.bot.db.update_status(interaction.guild_id, id, "cancelled")
        await interaction.response.send_message(f"Cancelled question **#{id}**.", delete_after=20)

async def setup(bot: commands.Bot):
    await bot.add_cog(AskHistCog(bot))
