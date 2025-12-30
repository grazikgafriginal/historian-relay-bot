from __future__ import annotations

import discord

class AnswerModal(discord.ui.Modal, title="Publish Answer"):
    answer = discord.ui.TextInput(
        label="Answer",
        style=discord.TextStyle.paragraph,
        min_length=20,
        max_length=4000,
        placeholder="Write a thorough answer. Add dates, places, and (optionally) sources.",
    )

    def __init__(self, on_submit_cb):
        super().__init__(timeout=600)
        self._on_submit_cb = on_submit_cb

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._on_submit_cb(interaction, str(self.answer))

class DenyModal(discord.ui.Modal, title="Deny Question"):
    reason = discord.ui.TextInput(
        label="Reason (optional)",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=500,
        placeholder="Optional: short reason for denial.",
    )

    def __init__(self, on_submit_cb):
        super().__init__(timeout=600)
        self._on_submit_cb = on_submit_cb

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._on_submit_cb(interaction, str(self.reason).strip() or None)

class CloseReasonModal(discord.ui.Modal, title="Close as Unclear"):
    reason = discord.ui.TextInput(
        label="Reason (optional)",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=800,
        placeholder="Optional: explain what was unclear or what details are missing.",
    )

    def __init__(self, on_submit_cb):
        super().__init__(timeout=600)
        self._on_submit_cb = on_submit_cb

    async def on_submit(self, interaction: discord.Interaction) -> None:
        text = str(self.reason).strip()
        await self._on_submit_cb(interaction, text or None)  # <-- PUT IT HERE

