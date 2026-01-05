from __future__ import annotations

import asyncio
import logging
from typing import Optional
from historian_relay_bot.ui.modals import AnswerModal, DenyModal, CloseReasonModal

import discord

from historian_relay_bot.utils.formatting import build_forward_embed

log = logging.getLogger("historian_relay.views")

def has_role(member: discord.Member, role_id: int) -> bool:
    return any(r.id == role_id for r in member.roles)

class HistorianView(discord.ui.View):
    """
    Attached to the forwarded message...
    """

    def __init__(self, bot, qid: int):
        super().__init__(timeout=None)
        self.bot = bot
        self.qid = qid

    async def _load(self, guild_id: int):
        q = await self.bot.db.get_question(guild_id, self.qid)
        if not q:
            raise RuntimeError("Question not found.")
        return q

    def _perm_ok(self, member: discord.Member) -> bool:
        cfg = self.bot.cfg
        return has_role(member, cfg.MOD_ROLE_ID) or has_role(member, cfg.VERIFIED_HISTORIAN_ROLE_ID)

    async def _refresh_embed(self, interaction: discord.Interaction) -> None:
        q = await self._load(interaction.guild_id)
        origin_url = ""
        try:
            origin_ch = await self.bot.fetch_channel(int(q.origin_channel_id))
            origin_msg = await origin_ch.fetch_message(int(q.origin_message_id))
            origin_url = origin_msg.jump_url
        except Exception:
            origin_url = "https://discord.com"  # harmless fallback

        author = await self.bot.fetch_user(int(q.created_by_user_id))
        claimed_by_text = None
        if q.claimed_by_user_id:
            try:
                u = await self.bot.fetch_user(int(q.claimed_by_user_id))
                claimed_by_text = u.mention
            except Exception:
                claimed_by_text = f"`{q.claimed_by_user_id}`"

        embed = build_forward_embed(
            qid=q.id,
            question_text=q.question_text,
            author=author,
            origin_jump_url=origin_url,
            tag=q.tag,
            era=q.era,
            status=q.status,
            claimed_by_text=claimed_by_text,  # <-- NEW
        )

        await interaction.message.edit(embed=embed, view=self)

    @discord.ui.button(label="✅ Claim", style=discord.ButtonStyle.success, custom_id="askhist:claim")
    async def claim_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return
        if not self._perm_ok(interaction.user):
            return await interaction.response.send_message("You don't have permission to claim.", delete_after=20)

        ok = await self.bot.db.try_claim(interaction.guild_id, self.qid, interaction.user.id)
        if not ok:
            return await interaction.response.send_message("This question is already claimed or not claimable.", delete_after=20)

        await interaction.response.send_message(f"Claimed Question #{self.qid}.", delete_after=20)
        await self._refresh_embed(interaction)

    @discord.ui.button(label="❌ Unclaim", style=discord.ButtonStyle.secondary, custom_id="askhist:unclaim")
    async def unclaim_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return
        if not self._perm_ok(interaction.user):
            return await interaction.response.send_message("You don't have permission to unclaim.", delete_after=20)

        q = await self._load(interaction.guild_id)
        cfg = self.bot.cfg
        is_mod = has_role(interaction.user, cfg.MOD_ROLE_ID)
        if q.claimed_by_user_id and (int(q.claimed_by_user_id) != interaction.user.id) and not is_mod:
            return await interaction.response.send_message("Only the claim owner (or a moderator) can unclaim.", delete_after=20)

        await self.bot.db.unclaim(interaction.guild_id, self.qid)
        await interaction.response.send_message(f"Unclaimed Question #{self.qid}.", delete_after=20)
        await self._refresh_embed(interaction)

    @discord.ui.button(label="📝 Publish Answer", style=discord.ButtonStyle.primary, custom_id="askhist:publish")
    async def publish_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return
        if not self._perm_ok(interaction.user):
            return await interaction.response.send_message("You don't have permission to publish.", delete_after=20)

        q = await self._load(interaction.guild_id)
        cfg = self.bot.cfg

        if not cfg.ALLOW_PUBLISH_WITHOUT_CLAIM:
            if not q.claimed_by_user_id:
                return await interaction.response.send_message("This question must be **claimed** before publishing (server setting).", delete_after=20)
            if int(q.claimed_by_user_id) != interaction.user.id and not has_role(interaction.user, cfg.MOD_ROLE_ID):
                return await interaction.response.send_message("Only the claim owner (or a moderator) can publish.", delete_after=20)

        # Mode A: reply-based publish (search recent messages for a reply to the forwarded embed)
        answer_text: Optional[str] = None
        try:
            async for msg in interaction.channel.history(limit=50):
                if msg.author.id != interaction.user.id:
                    continue
                if msg.reference and msg.reference.message_id == interaction.message.id and msg.content:
                    answer_text = msg.content.strip()
                    break
        except Exception:
            pass

        if answer_text:
            await interaction.response.defer(thinking=True)
            await self.bot.publish_answer(interaction, qid=self.qid, answer_text=answer_text)
            return

        # Mode B: modal publish
        async def on_modal_submit(modal_interaction: discord.Interaction, text: str):
            await modal_interaction.response.defer(thinking=True)
            await self.bot.publish_answer(modal_interaction, qid=self.qid, answer_text=text)

        async for msg in interaction.channel.history(limit=50):

            await interaction.response.send_modal(AnswerModal(on_modal_submit))

    @discord.ui.button(label="❓ Needs Context", style=discord.ButtonStyle.secondary, custom_id="askhist:needs_context")
    async def needs_context_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return
        if not self._perm_ok(interaction.user):
            return await interaction.response.send_message("You don't have permission to do that.", delete_after=20)

        await self.bot.db.update_status(interaction.guild_id, self.qid, "needs_context")
        await interaction.response.send_message("Marked as needs context.", delete_after=20)
        await self.bot.post_needs_context(interaction.guild_id, self.qid)
        await self._refresh_embed(interaction)

    @discord.ui.button(label="🗑 Close as Unclear", style=discord.ButtonStyle.danger, custom_id="askhist:close")
    async def close_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return
        if not self._perm_ok(interaction.user):
            return await interaction.response.send_message("You don't have permission to close.", delete_after=20)

        await self.bot.db.update_status(interaction.guild_id, self.qid, "closed")
        await self.bot.post_closed_unclear(interaction.guild_id, self.qid, closed_by_id=interaction.user.id, reason=None)
        self.bot.schedule_cleanup(interaction.guild_id, self.qid, delay_seconds=60)
        await interaction.response.send_message("Closed as unclear.", delete_after=20)
        await self._refresh_embed(interaction)
    
        
    @discord.ui.button(label="📝 Close w/ Reason", style=discord.ButtonStyle.danger, custom_id="askhist:close_reason")
    async def close_reason_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return

        if not self._perm_ok(interaction.user):
            return await interaction.response.send_message(
                "You don't have permission to close.",
                delete_after=20
            )

        async def on_submit(modal_interaction: discord.Interaction, reason: str | None):
            try:
                await modal_interaction.response.defer(thinking=True)

                # close + post message to thread/origin
                await self.bot.db.update_status(modal_interaction.guild_id, self.qid, "closed")
                await self.bot.post_closed_unclear(
                    modal_interaction.guild_id,
                    self.qid,
                    closed_by_id=modal_interaction.user.id,
                    reason=reason,
                )

                # refresh the historians embed (modal interactions don't have modal_interaction.message)
                q = await self.bot.db.get_question(modal_interaction.guild_id, self.qid)
                if q and q.hist_message_id:
                    hist_ch = await self.bot.fetch_channel(self.bot.cfg.HISTORIANS_CHANNEL_ID)
                    hist_msg = await hist_ch.fetch_message(int(q.hist_message_id))

                    # reuse your existing refresh logic but edit hist_msg directly
                    q = await self._load(modal_interaction.guild_id)

                    origin_url = "https://discord.com"
                    try:
                        origin_ch = await self.bot.fetch_channel(int(q.origin_channel_id))
                        origin_msg = await origin_ch.fetch_message(int(q.origin_message_id))
                        origin_url = origin_msg.jump_url
                    except Exception:
                        pass

                    author = await self.bot.fetch_user(int(q.created_by_user_id))
                    claimed_by_text = None
                    if q.claimed_by_user_id:
                        try:
                            u = await self.bot.fetch_user(int(q.claimed_by_user_id))
                            claimed_by_text = u.mention
                        except Exception:
                            claimed_by_text = f"`{q.claimed_by_user_id}`"

                    embed = build_forward_embed(
                        qid=q.id,
                        question_text=q.question_text,
                        author=author,
                        origin_jump_url=origin_url,
                        tag=q.tag,
                        era=q.era,
                        status=q.status,
                        claimed_by_text=claimed_by_text,
                    )

                    await hist_msg.edit(embed=embed, view=self)

                # IMPORTANT: followup.send does NOT support delete_after in your environment
                msg = await modal_interaction.followup.send("Closed as unclear (with reason).", wait=True)
                await msg.delete(delay=20)
                self.bot.schedule_cleanup(modal_interaction.guild_id, self.qid, delay_seconds=60)

            except Exception as e:
                # ensure the modal interaction finishes (avoid infinite "thinking")
                try:
                    msg = await modal_interaction.followup.send(f"Close failed: `{type(e).__name__}`", wait=True)
                    await msg.delete(delay=20)
                except Exception:
                    pass
                raise

        # Open modal immediately (this ACKs the button click)
        await interaction.response.send_modal(CloseReasonModal(on_submit))


        

class QueueView(discord.ui.View):
    """
    Attached to the forwarded message...
    """
    def __init__(self, bot, qid: int):
        super().__init__(timeout=None)
        self.bot = bot
        self.qid = qid

    def _is_mod(self, member: discord.Member) -> bool:
        return any(r.id == self.bot.cfg.MOD_ROLE_ID for r in member.roles)

    @discord.ui.button(label="✅ Approve", style=discord.ButtonStyle.success, custom_id="askhist:approve")
    async def approve_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return
        if not self._is_mod(interaction.user):
            return await interaction.response.send_message("Only moderators can approve.", delete_after=20)

        await interaction.response.defer( thinking=True)
        await self.bot.approve_from_queue(interaction.guild_id, self.qid)
        await interaction.followup.send(f"Approved Question #{self.qid} and forwarded to historians.", delete_after=20)

    @discord.ui.button(label="❌ Deny", style=discord.ButtonStyle.danger, custom_id="askhist:deny")
    async def deny_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return
        if not self._is_mod(interaction.user):
            return await interaction.response.send_message("Only moderators can deny.", delete_after=20)

        async def on_deny_submit(modal_interaction: discord.Interaction, reason: str | None):
            await modal_interaction.response.defer(thinking=True)
            await self.bot.deny_from_queue(modal_interaction.guild_id, self.qid, reason=reason)
            await modal_interaction.followup.send(f"Denied Question #{self.qid}.", delete_after=20)

        await interaction.response.send_modal(DenyModal(on_deny_submit))
