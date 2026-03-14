from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Optional
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from historian_relay_bot.cogs.askhist import TAG_CHOICES, ERA_CHOICES

log = logging.getLogger("historian_relay.suggest")

SUGGESTIONS_FILE = Path(__file__).resolve().parent.parent / "data" / "suggestions.json"
_suggestions_lock = asyncio.Lock()


def _ensure_file() -> None:
    SUGGESTIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not SUGGESTIONS_FILE.exists():
        SUGGESTIONS_FILE.write_text("[]", encoding="utf-8")


def _load_suggestions() -> list[dict]:
    _ensure_file()
    try:
        raw = SUGGESTIONS_FILE.read_text(encoding="utf-8").strip()
        if not raw:
            return []
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        log.warning("suggestions.json was unreadable; resetting to empty list")
        return []


def _save_suggestions(data: list[dict]) -> None:
    _ensure_file()
    tmp = SUGGESTIONS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(SUGGESTIONS_FILE)
def _format_suggestion_time(ts: int) -> str:
    if not ts:
        return "Unknown time"
    return discord.utils.format_dt(
        datetime.fromtimestamp(ts, tz=timezone.utc),
        style="R",
    )


def _build_admin_suggestions_embed(
    suggestions: list[dict],
    target_user: discord.Member | None = None,
) -> discord.Embed:
    title = (
        f"Suggestions by {target_user.display_name}"
        if target_user
        else "Recent Suggestions"
    )

    embed = discord.Embed(
        title=title,
        description=f"Showing the latest {min(len(suggestions), 10)} suggestion(s).",
    )

    for item in suggestions[:10]:
        question = str(item.get("question_text", "Unknown question")).strip()
        if len(question) > 200:
            question = question[:197] + "..."

        username = item.get("username", "Unknown user")
        tag = item.get("tag") or "—"
        era = item.get("era") or "—"
        status = item.get("status") or "pending"
        created_at = int(item.get("created_at", 0))

        embed.add_field(
            name=f"#{item.get('id', '?')} • {username}",
            value=(
                f"**Question:** {question}\n"
                f"**Tag:** {tag}\n"
                f"**Era:** {era}\n"
                f"**Status:** {status}\n"
                f"**Submitted:** {_format_suggestion_time(created_at)}"
            ),
            inline=False,
        )

    embed.set_footer(text="Use !suggestions @user to filter by one user.")
    return embed


class SuggestCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _allowed_channel(self, channel_id: int) -> bool:
        return channel_id in self.bot.cfg.SUBMISSION_CHANNEL_IDS

    @app_commands.command(
        name="suggest",
        description="Suggest a history question to be saved for later review."
    )
    @app_commands.describe(
        question="The question you want to suggest",
        tag="Optional topic tag",
        era="Optional era"
    )
    @app_commands.choices(tag=TAG_CHOICES, era=ERA_CHOICES)
    async def suggest(
        self,
        interaction: discord.Interaction,
        question: str,
        tag: Optional[app_commands.Choice[str]] = None,
        era: Optional[app_commands.Choice[str]] = None,
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        question = question.strip()
        if len(question) < self.bot.cfg.MIN_QUESTION_LENGTH:
            await interaction.response.send_message(
                f"Your suggestion is too short. Minimum length is {self.bot.cfg.MIN_QUESTION_LENGTH} characters.",
                ephemeral=True,
            )
            return

        tag_val = tag.value if tag else None
        era_val = era.value if era else None

        async with _suggestions_lock:
            data = _load_suggestions()
            next_id = max((int(item.get("id", 0)) for item in data), default=0) + 1

            suggestion = {
                "id": next_id,
                "guild_id": str(interaction.guild_id),
                "channel_id": str(interaction.channel_id),
                "user_id": str(interaction.user.id),
                "username": str(interaction.user),
                "question_text": question,
                "tag": tag_val,
                "era": era_val,
                "status": "pending",
                "created_at": int(time.time()),
            }

            data.append(suggestion)
            _save_suggestions(data)

        await interaction.response.send_message(
            f"Suggestion saved as **#{next_id}**.",
            ephemeral=True,
        )

    @commands.command(name="suggestions")
    async def suggestions_cmd(
        self,
        ctx: commands.Context,
        user: discord.Member | None = None,
    ):
        if not ctx.guild or not isinstance(ctx.author, discord.Member):
            return

        if not ctx.author.guild_permissions.administrator:
            return await ctx.send("Admins only.", delete_after=20)

        data = _load_suggestions()

        if user is not None:
            data = [item for item in data if str(item.get("user_id")) == str(user.id)]

        data.sort(key=lambda x: int(x.get("created_at", 0)), reverse=True)

        if not data:
            if user is not None:
                return await ctx.send(
                    f"No suggestions found for {user.mention}.",
                    delete_after=20,
                )
            return await ctx.send("No suggestions found.", delete_after=20)

        embed = _build_admin_suggestions_embed(data, target_user=user)
        await ctx.send(embed=embed)

async def setup(bot: commands.Bot):
    await bot.add_cog(SuggestCog(bot))