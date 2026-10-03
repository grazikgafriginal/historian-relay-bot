from __future__ import annotations

import datetime
import json
import logging
import random
from pathlib import Path
from typing import Any, Optional, Union

import discord
from discord import app_commands
from discord.ext import commands, tasks

from historian_relay_bot.utils.formatting import build_topic_of_day_embed

log = logging.getLogger("historian_relay.topic_of_day")


class TopicOfDayCog(commands.Cog):
    """Posts an open-ended historical discussion prompt on a schedule.

    Unlike /askhist or GuessYear, there's no "correct answer" here — this is
    purely meant to give #history-general (or wherever it's configured) a
    recurring reason to start a conversation.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._topics: list[dict[str, Any]] = []
        self._load_dataset()

    def _is_mod(self, member: discord.Member) -> bool:
        return any(r.id == self.bot.cfg.MOD_ROLE_ID for r in member.roles)

    def _load_dataset(self) -> None:
        data_path = Path(__file__).resolve().parent.parent / "data" / "topics.json"
        if not data_path.exists():
            log.error("Topic of the Day dataset missing at %s", data_path)
            self._topics = []
            return

        try:
            raw = json.loads(data_path.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                raise ValueError("dataset must be a list of topics")
            self._topics = [t for t in raw if isinstance(t, dict) and "id" in t and "prompt" in t]
            log.info("Loaded Topic of the Day dataset: %d topics", len(self._topics))
        except Exception:
            log.exception("Failed to load Topic of the Day dataset")
            self._topics = []

    async def cog_load(self) -> None:
        if self.bot.cfg.TOPIC_OF_DAY_CHANNEL_ID and not self._daily_loop.is_running():
            self._daily_loop.start()

    async def cog_unload(self) -> None:
        self._daily_loop.cancel()

    async def _pick_topic(self, guild_id: int) -> Optional[dict[str, Any]]:
        if not self._topics:
            return None
        recent_ids = set(await self.bot.db.topic_log_recent_topic_ids(guild_id, max(0, len(self._topics) - 1)))
        pool = [t for t in self._topics if t["id"] not in recent_ids]
        if not pool:
            pool = self._topics  # whole pool has cycled through — start over
        return random.choice(pool)

    async def _post_topic(
        self, channel: Union[discord.TextChannel, discord.Thread], *, date_key: str, auto: bool
    ) -> Optional[discord.Message]:
        guild = channel.guild
        if guild is None:
            return None

        topic = await self._pick_topic(guild.id)
        if not topic:
            log.warning("No Topic of the Day topics available for guild %s", guild.id)
            return None

        embed = build_topic_of_day_embed(topic, date_key)
        msg = await channel.send(embed=embed)

        await self.bot.db.topic_log_record(
            guild_id=guild.id,
            channel_id=channel.id,
            topic_id=str(topic["id"]),
            message_id=msg.id,
            date_key=date_key,
            auto=auto,
        )

        try:
            await msg.pin()
        except Exception:
            pass  # non-essential (e.g. channel already has 50 pins)

        return msg

    @tasks.loop(minutes=5)
    async def _daily_loop(self) -> None:
        now = datetime.datetime.now(datetime.timezone.utc)
        date_key = now.strftime("%Y-%m-%d")
        if now.hour < self.bot.cfg.TOPIC_OF_DAY_HOUR_UTC:
            return

        channel_id = self.bot.cfg.TOPIC_OF_DAY_CHANNEL_ID
        if not channel_id:
            return

        channel = self.bot.get_channel(channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)) or channel.guild is None:
            return

        if await self.bot.db.topic_log_already_posted_today(channel.guild.id, date_key):
            return

        try:
            await self._post_topic(channel, date_key=date_key, auto=True)
        except Exception:
            log.exception("Failed to post automatic Topic of the Day")

    @_daily_loop.before_loop
    async def _before_daily_loop(self) -> None:
        await self.bot.wait_until_ready()

    @app_commands.command(name="topic_now", description="Post a Topic of the Day immediately (mods only).")
    @app_commands.describe(channel="Channel to post in (defaults to the configured Topic of the Day channel)")
    async def topic_now(self, interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("Server only.", ephemeral=True)
        if not self._is_mod(interaction.user):
            return await interaction.response.send_message("Mods only.", ephemeral=True)

        target: Optional[Union[discord.TextChannel, discord.Thread]] = channel
        if target is None and self.bot.cfg.TOPIC_OF_DAY_CHANNEL_ID:
            maybe = self.bot.get_channel(self.bot.cfg.TOPIC_OF_DAY_CHANNEL_ID)
            if isinstance(maybe, (discord.TextChannel, discord.Thread)):
                target = maybe
        if target is None and isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
            target = interaction.channel

        if target is None:
            return await interaction.response.send_message(
                "No channel to post in — set TOPIC_OF_DAY_CHANNEL_ID or pick a channel.", ephemeral=True
            )

        if not self._topics:
            return await interaction.response.send_message(
                "No topics are loaded — check historian_relay_bot/data/topics.json.", ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)
        now = datetime.datetime.now(datetime.timezone.utc)
        msg = await self._post_topic(target, date_key=now.strftime("%Y-%m-%d"), auto=False)
        if msg:
            await interaction.followup.send(f"Posted a topic in {target.mention}: {msg.jump_url}", ephemeral=True)
        else:
            await interaction.followup.send("Failed to post a topic — check the logs.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(TopicOfDayCog(bot))
