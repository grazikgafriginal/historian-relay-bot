from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import discord
from discord import app_commands
from discord.ext import commands

from historian_relay_bot.utils.formatting import build_source_post_text
from historian_relay_bot.utils.source_filename import (
    build_author_part,
    build_filename,
    build_short_title,
    build_surname,
    is_valid_filename_part,
    main_title,
    normalize_for_dup,
    parse_authors,
)

log = logging.getLogger("historian_relay.book_sources")

TOPIC_CODES = [
    ("GEN", "General histories & overviews"),
    ("ORIG", "Origins & road to war (1919–39)"),
    ("WEST", "Western Europe"),
    ("EAST", "Eastern Front"),
    ("MED", "Mediterranean & North Africa"),
    ("PAC", "Asia-Pacific war"),
    ("NAVAL", "War at sea"),
    ("AIR", "Air war"),
    ("ECON", "War economies & production"),
    ("OCC", "Occupation, Holocaust & genocide"),
    ("DIPL", "Diplomacy, alliances & leadership"),
]
TOPIC_CHOICES = [app_commands.Choice(name=label, value=code) for code, label in TOPIC_CODES]

TYPE_LABELS = [
    "General history",
    "Academic monograph",
    "Edited volume",
    "Memoir / primary source",
    "Reference work",
    "Article",
]
TYPE_CHOICES = [app_commands.Choice(name=t, value=t) for t in TYPE_LABELS]
SOURCE_TYPE_EDITED_VOLUME = "Edited volume"


def _not_forum_channel_hint(ch: Optional[discord.abc.Snowflake]) -> str:
    """A specific, actionable message for the #1 way to misconfigure
    SOURCES_FORUM_CHANNEL_ID: pointing it at one of the era posts (which are
    threads under the hood) instead of the forum channel that contains them."""
    if isinstance(ch, discord.Thread):
        parent_hint = ""
        if isinstance(ch.parent, discord.ForumChannel):
            parent_hint = f" Its parent forum channel's id is {ch.parent.id} — use that instead."
        return (
            f"SOURCES_FORUM_CHANNEL_ID ({ch.id}) points at a post/thread (\"{ch.name}\") inside a "
            f"forum, not the forum channel itself.{parent_hint}"
        )
    kind = type(ch).__name__ if ch is not None else "unknown"
    return f"SOURCES_FORUM_CHANNEL_ID does not point at a forum channel (it resolved to a {kind})."


def _resolve_guild_id() -> Optional[int]:
    """Mirrors config.py's env-or-CONFIG_JSON resolution for one setting.

    Needed because app_commands.guilds(...) must be applied at module-import
    time (before the bot/cfg object exists), so this can't just read bot.cfg.
    """
    raw = os.getenv("GUILD_ID")
    if not raw:
        cfg_path = os.getenv("CONFIG_JSON")
        if cfg_path and Path(cfg_path).exists():
            try:
                raw = json.loads(Path(cfg_path).read_text(encoding="utf-8")).get("GUILD_ID")
            except Exception:
                raw = None
    try:
        return int(raw) if raw else None
    except (TypeError, ValueError):
        return None


_GUILD_ID = _resolve_guild_id()


class RejectReasonModal(discord.ui.Modal, title="Reject Submission"):
    reason = discord.ui.TextInput(
        label="Reason",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=300,
        placeholder="Why is this submission being rejected?",
    )

    def __init__(self, on_submit_cb):
        super().__init__(timeout=300)
        self._on_submit_cb = on_submit_cb

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._on_submit_cb(interaction, str(self.reason).strip())


class EditFilenameModal(discord.ui.Modal, title="Edit Filename"):
    def __init__(self, current_author: str, current_short_title: str, on_submit_cb):
        super().__init__(timeout=300)
        self._on_submit_cb = on_submit_cb
        self.author_part = discord.ui.TextInput(
            label="Author part",
            default=current_author,
            max_length=80,
            required=True,
        )
        self.short_title_part = discord.ui.TextInput(
            label="Short-title part",
            default=current_short_title,
            max_length=80,
            required=True,
        )
        self.add_item(self.author_part)
        self.add_item(self.short_title_part)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._on_submit_cb(interaction, str(self.author_part).strip(), str(self.short_title_part).strip())


class SourceReviewView(discord.ui.View):
    """Attached to a submission's review-channel message.

    Follows this repo's existing pattern (HistorianView/QueueView in
    ui/views.py): persistent (timeout=None), re-attached fresh to each
    tracked message on restart rather than relying on Discord-side
    global persistent-view dispatch. custom_ids still encode the
    submission id, for debuggability.
    """

    def __init__(self, cog: "BookSourcesCog", submission_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.submission_id = submission_id

        approve = discord.ui.Button(
            label="✅ Approve", style=discord.ButtonStyle.success,
            custom_id=f"srcsub:approve:{submission_id}",
        )
        approve.callback = self._on_approve
        self.add_item(approve)

        reject = discord.ui.Button(
            label="❌ Reject", style=discord.ButtonStyle.danger,
            custom_id=f"srcsub:reject:{submission_id}",
        )
        reject.callback = self._on_reject
        self.add_item(reject)

        edit_fn = discord.ui.Button(
            label="✏️ Edit filename", style=discord.ButtonStyle.secondary,
            custom_id=f"srcsub:editfn:{submission_id}",
        )
        edit_fn.callback = self._on_edit_filename
        self.add_item(edit_fn)

    def _is_reviewer(self, member: discord.Member) -> bool:
        return any(r.id == self.cog.bot.cfg.REVIEWER_ROLE_ID for r in member.roles)

    async def _on_approve(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return
        if not self._is_reviewer(interaction.user):
            return await interaction.response.send_message("Only reviewers can do that.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        _ok, msg = await self.cog.approve_submission(interaction.guild_id, self.submission_id, reviewer=interaction.user)
        await interaction.followup.send(msg, ephemeral=True)

    async def _on_reject(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return
        if not self._is_reviewer(interaction.user):
            return await interaction.response.send_message("Only reviewers can do that.", ephemeral=True)

        async def on_submit(modal_interaction: discord.Interaction, reason: str):
            await modal_interaction.response.defer(ephemeral=True)
            _ok, msg = await self.cog.reject_submission(
                modal_interaction.guild_id, self.submission_id,
                reviewer=modal_interaction.user, reason=reason,
            )
            await modal_interaction.followup.send(msg, ephemeral=True)

        await interaction.response.send_modal(RejectReasonModal(on_submit))

    async def _on_edit_filename(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return
        if not self._is_reviewer(interaction.user):
            return await interaction.response.send_message("Only reviewers can do that.", ephemeral=True)

        row = await self.cog.bot.db.source_submission_get(interaction.guild_id, self.submission_id)
        if not row:
            return await interaction.response.send_message("Submission not found.", ephemeral=True)

        async def on_submit(modal_interaction: discord.Interaction, author_part: str, short_title_part: str):
            await modal_interaction.response.defer(ephemeral=True)
            _ok, msg = await self.cog.edit_filename(
                modal_interaction.guild_id, self.submission_id,
                author_part=author_part, short_title_part=short_title_part,
            )
            await modal_interaction.followup.send(msg, ephemeral=True)

        await interaction.response.send_modal(
            EditFilenameModal(row.filename_author or "", row.filename_short_title or "", on_submit)
        )


class BookSourcesCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._recovery_task: Optional[asyncio.Task] = None

    async def cog_load(self) -> None:
        self._recovery_task = asyncio.create_task(self._startup_recovery())

    async def cog_unload(self) -> None:
        if self._recovery_task:
            self._recovery_task.cancel()

    # --------------------------
    # Restart recovery + topic thread check
    # --------------------------

    async def _startup_recovery(self) -> None:
        await self.bot.wait_until_ready()
        for guild in list(self.bot.guilds):
            try:
                await self.bot.db.source_submission_cleanup_stuck_approving(guild.id)
                rows = await self.bot.db.source_submission_list_restorable(guild.id)
                if not rows:
                    continue
                review_ch = await self.bot.fetch_channel(self.bot.cfg.REVIEW_CHANNEL_ID)
                for row in rows:
                    try:
                        msg = await review_ch.fetch_message(int(row.review_message_id))
                        await msg.edit(view=SourceReviewView(self, row.id))
                        await asyncio.sleep(0.2)
                    except Exception:
                        continue
            except Exception:
                log.exception("Book sources: startup recovery failed for guild %s", guild.id)

        await self._check_topic_threads()

    async def _check_topic_threads(self) -> None:
        """Startup sanity check: log a warning for any topic code with no resolvable
        destination thread, same spirit as the old forum-tag check this replaced."""
        try:
            forum = await self.bot.fetch_channel(self.bot.cfg.SOURCES_FORUM_CHANNEL_ID)
        except Exception:
            log.warning("Book sources: could not fetch SOURCES_FORUM_CHANNEL_ID to check topic threads.")
            return
        if not isinstance(forum, discord.ForumChannel):
            log.warning("Book sources: %s", _not_forum_channel_hint(forum))
            return

        for code, _label in TOPIC_CODES:
            thread = await self._resolve_topic_thread(forum, code)
            if thread is None:
                log.warning(
                    "Book sources: no forum thread found for topic %r — create one (named so it "
                    "contains %r) or set SOURCE_TOPIC_THREAD_IDS.", code, code,
                )

    async def _find_topic_thread_by_name(self, forum: discord.ForumChannel, topic_code: str) -> Optional[discord.Thread]:
        pattern = re.compile(rf"\b{re.escape(topic_code)}\b", re.IGNORECASE)
        matches = [t for t in forum.threads if pattern.search(t.name)]
        seen_ids = {t.id for t in matches}
        try:
            async for t in forum.archived_threads(limit=None):
                if t.id not in seen_ids and pattern.search(t.name):
                    matches.append(t)
                    seen_ids.add(t.id)
        except Exception:
            log.warning("Book sources: failed to list archived threads while searching for topic %r.", topic_code)

        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            log.warning(
                "Book sources: multiple forum threads match topic %r (%s) — ambiguous, set "
                "SOURCE_TOPIC_THREAD_IDS to disambiguate.", topic_code, ", ".join(t.name for t in matches),
            )
        return None

    async def _resolve_topic_thread(self, forum: discord.ForumChannel, topic_code: str) -> Optional[discord.Thread]:
        """The era/topic thread approved books for this code get posted into.

        Checks SOURCE_TOPIC_THREAD_IDS first (explicit, reliable), then falls back to
        finding an existing thread in the forum whose name contains the topic code —
        so a server with pre-existing era threads needs no config at all.
        """
        thread_id = self.bot.cfg.SOURCE_TOPIC_THREAD_IDS.get(topic_code)
        if thread_id:
            try:
                ch = await self.bot.fetch_channel(thread_id)
                if isinstance(ch, discord.Thread):
                    return ch
                log.warning("Book sources: SOURCE_TOPIC_THREAD_IDS[%r]=%s is not a thread.", topic_code, thread_id)
            except Exception:
                log.warning(
                    "Book sources: configured thread id %s for topic %r could not be fetched.",
                    thread_id, topic_code,
                )

        return await self._find_topic_thread_by_name(forum, topic_code)

    # --------------------------
    # /submit
    # --------------------------

    def _validate_submission(
        self,
        *,
        title: str,
        authors: list[str],
        publisher: str,
        year: int,
        covers: str,
        topic: str,
        secondary_topic: Optional[str],
        original_year: Optional[int],
        file: Optional[discord.Attachment],
        link: Optional[str],
        guild: discord.Guild,
    ) -> list[str]:
        errors: list[str] = []

        if not title.strip():
            errors.append("Title is required.")
        elif len(title) > 300:
            errors.append("Title must be 300 characters or fewer.")

        if not authors:
            errors.append("At least one author is required.")
        elif any(len(a) > 100 for a in authors):
            errors.append("Each author name must be 100 characters or fewer.")

        if not publisher.strip():
            errors.append("Publisher is required.")
        elif len(publisher) > 200:
            errors.append("Publisher must be 200 characters or fewer.")

        current_year = datetime.now(timezone.utc).year
        if not (1800 <= year <= current_year):
            errors.append(f"Year must be between 1800 and {current_year}.")

        if original_year is not None and not (1800 <= original_year <= year):
            errors.append(f"Original year must be between 1800 and the submitted edition's year ({year}).")

        if not covers.strip():
            errors.append("Covers is required.")
        elif len(covers) > 80:
            errors.append("Covers must be 80 characters or fewer.")

        if secondary_topic is not None and secondary_topic == topic:
            errors.append("Secondary topic must differ from the primary topic.")

        if file is not None:
            ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
            if ext not in self.bot.cfg.ALLOWED_EXTENSIONS:
                allowed = ", ".join(self.bot.cfg.ALLOWED_EXTENSIONS)
                errors.append(f"File must be one of: {allowed}.")
            if file.size > guild.filesize_limit:
                limit_mb = guild.filesize_limit // (1024 * 1024)
                errors.append(f"File is too large for this server's upload limit ({limit_mb} MB).")

        if link is not None and link.strip():
            parsed = urlparse(link.strip())
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                errors.append("Link must be a valid http(s) URL.")

        return errors

    async def _is_duplicate(self, guild_id: int, first_author: str, title: str) -> bool:
        target_surname = normalize_for_dup(build_surname(first_author))
        target_title = normalize_for_dup(main_title(title))

        rows = await self.bot.db.source_submission_list_active(guild_id)
        for r in rows:
            r_authors = parse_authors(str(r["authors"]))
            if not r_authors:
                continue
            r_surname = normalize_for_dup(build_surname(r_authors[0]))
            r_title = normalize_for_dup(main_title(str(r["title"])))
            if r_surname == target_surname and r_title == target_title:
                return True
        return False

    def _render_preview(
        self,
        row,
        *,
        filename_author: Optional[str] = None,
        filename_short_title: Optional[str] = None,
    ) -> str:
        authors = parse_authors(row.authors)
        is_edited = row.type == SOURCE_TYPE_EDITED_VOLUME

        filename = None
        if row.file_ext:
            fa = filename_author if filename_author is not None else row.filename_author
            fst = filename_short_title if filename_short_title is not None else row.filename_short_title
            filename = build_filename(
                prefix=self.bot.cfg.SOURCE_TOPIC_PREFIX, topic_code=row.topic,
                author_part=fa, year=row.year, short_title_part=fst, ext=row.file_ext,
            )

        return build_source_post_text(
            title=row.title, authors=authors, is_edited=is_edited,
            publisher=row.publisher, year=row.year, original_year=row.original_year,
            topic=row.topic, secondary_topic=row.secondary_topic, covers=row.covers,
            work_type=row.type, filename=filename, link=row.link,
            submitted_by_user_id=int(row.user_id),
        )

    @app_commands.command(name="submit", description="Submit a book source for review.")
    @app_commands.describe(
        title="Full title, including subtitle",
        author='Full name(s). Separate multiple people with ";"',
        publisher="Publisher",
        year="Year of the edition being submitted",
        topic="Primary topic",
        covers='Short scope, e.g. "Germany, 1933–1945" (max 80 chars)',
        type_="Type of work",
        secondary_topic="Optional secondary topic (must differ from the primary topic)",
        original_year="Optional: original publication year, for translations/later editions",
        file="Optional: the book file (must be an allowed extension and within the server's upload limit)",
        link="Optional: a link to the source",
    )
    @app_commands.choices(topic=TOPIC_CHOICES, secondary_topic=TOPIC_CHOICES, type_=TYPE_CHOICES)
    @app_commands.rename(type_="type")
    async def submit(
        self,
        interaction: discord.Interaction,
        title: str,
        author: str,
        publisher: str,
        year: int,
        topic: app_commands.Choice[str],
        covers: str,
        type_: app_commands.Choice[str],
        secondary_topic: Optional[app_commands.Choice[str]] = None,
        original_year: Optional[int] = None,
        file: Optional[discord.Attachment] = None,
        link: Optional[str] = None,
    ):
        if not interaction.guild:
            return await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)

        authors = parse_authors(author)
        secondary_topic_val = secondary_topic.value if secondary_topic else None

        errors = self._validate_submission(
            title=title, authors=authors, publisher=publisher, year=year, covers=covers,
            topic=topic.value, secondary_topic=secondary_topic_val,
            original_year=original_year, file=file, link=link, guild=interaction.guild,
        )
        if errors:
            bullets = "\n".join(f"• {e}" for e in errors)
            return await interaction.response.send_message(
                f"Please fix the following and resubmit:\n{bullets}", ephemeral=True
            )

        pending_count = await self.bot.db.source_submission_count_pending(interaction.guild_id, interaction.user.id)
        if pending_count >= self.bot.cfg.MAX_PENDING_PER_USER:
            return await interaction.response.send_message(
                f"You already have {pending_count} pending submission(s) — the limit is "
                f"{self.bot.cfg.MAX_PENDING_PER_USER}. Wait for one to be reviewed before submitting another.",
                ephemeral=True,
            )

        if await self._is_duplicate(interaction.guild_id, authors[0], title):
            return await interaction.response.send_message(
                "A pending or approved submission already exists with this author and title.", ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        is_edited = type_.value == SOURCE_TYPE_EDITED_VOLUME
        filename_author = build_author_part(authors, is_edited=is_edited)
        filename_short_title = build_short_title(title)

        file_ext: Optional[str] = None
        file_bytes: Optional[bytes] = None
        if file is not None:
            file_ext = file.filename.rsplit(".", 1)[-1].lower()
            file_bytes = await file.read()

        sub_id = await self.bot.db.source_submission_create(
            guild_id=interaction.guild_id,
            user_id=interaction.user.id,
            title=title.strip(),
            authors=author.strip(),
            publisher=publisher.strip(),
            year=year,
            original_year=original_year,
            topic=topic.value,
            secondary_topic=secondary_topic_val,
            covers=covers.strip(),
            type=type_.value,
            link=link.strip() if link else None,
            filename_author=filename_author,
            filename_short_title=filename_short_title,
            file_ext=file_ext,
        )

        row = await self.bot.db.source_submission_get(interaction.guild_id, sub_id)
        preview_text = self._render_preview(row)

        try:
            review_ch = await self.bot.fetch_channel(self.bot.cfg.REVIEW_CHANNEL_ID)
            discord_file = None
            if file_bytes is not None:
                generated_name = build_filename(
                    prefix=self.bot.cfg.SOURCE_TOPIC_PREFIX, topic_code=row.topic,
                    author_part=row.filename_author, year=row.year,
                    short_title_part=row.filename_short_title, ext=row.file_ext,
                )
                discord_file = discord.File(io.BytesIO(file_bytes), filename=generated_name)

            review_msg = await review_ch.send(
                content=preview_text,
                file=discord_file,
                view=SourceReviewView(self, sub_id),
            )
            await self.bot.db.source_submission_set_review_message(interaction.guild_id, sub_id, review_msg.id)
        except Exception:
            log.exception("Book sources: failed to post review message for submission #%s", sub_id)
            return await interaction.followup.send(
                "Your submission was saved, but I couldn't post it to the review channel — please contact a moderator.",
                ephemeral=True,
            )

        await interaction.followup.send("Submitted for review — you'll get a DM when it's decided.", ephemeral=True)

    # --------------------------
    # Review actions (called from SourceReviewView)
    # --------------------------

    async def approve_submission(self, guild_id: int, sub_id: int, *, reviewer: discord.Member) -> tuple[bool, str]:
        row = await self.bot.db.source_submission_get(guild_id, sub_id)
        if not row:
            return False, "Submission not found."
        if row.status != "pending":
            return False, f"This submission is no longer pending (status: `{row.status}`)."

        claimed = await self.bot.db.source_submission_try_start_approval(guild_id, sub_id, reviewer.id)
        if not claimed:
            return False, "This submission was already handled."

        review_msg = None
        try:
            review_ch = await self.bot.fetch_channel(self.bot.cfg.REVIEW_CHANNEL_ID)
            review_msg = await review_ch.fetch_message(int(row.review_message_id))

            discord_file = None
            if row.file_ext:
                if not review_msg.attachments:
                    raise RuntimeError("Expected attachment missing from the review message.")
                data = await review_msg.attachments[0].read()
                generated_name = build_filename(
                    prefix=self.bot.cfg.SOURCE_TOPIC_PREFIX, topic_code=row.topic,
                    author_part=row.filename_author, year=row.year,
                    short_title_part=row.filename_short_title, ext=row.file_ext,
                )
                discord_file = discord.File(io.BytesIO(data), filename=generated_name)

            forum = await self.bot.fetch_channel(self.bot.cfg.SOURCES_FORUM_CHANNEL_ID)
            if not isinstance(forum, discord.ForumChannel):
                raise RuntimeError(_not_forum_channel_hint(forum))

            thread = await self._resolve_topic_thread(forum, row.topic)
            if thread is None:
                raise RuntimeError(
                    f"No forum thread found for topic {row.topic} — create one (named so it contains "
                    f"\"{row.topic}\") or set SOURCE_TOPIC_THREAD_IDS."
                )
            if thread.archived:
                await thread.edit(archived=False)

            post_text = self._render_preview(row)
            posted = await thread.send(content=post_text, file=discord_file)
        except Exception as e:
            await self.bot.db.source_submission_revert_to_pending(guild_id, sub_id)
            log.warning("Book sources: approval failed for submission #%s, reverted to pending: %s", sub_id, e)
            return False, f"Approval failed and was rolled back — please retry (`{type(e).__name__}`: {e})"

        await self.bot.db.source_submission_mark_approved(
            guild_id, sub_id, reviewer_id=reviewer.id, forum_thread_id=thread.id, forum_message_id=posted.id
        )

        dm_failed = False
        try:
            user = await self.bot.fetch_user(int(row.user_id))
            await user.send(f"Your source submission **{row.title}** was approved! {posted.jump_url}")
        except Exception:
            dm_failed = True

        try:
            note = f"\n\n✅ **Approved by {reviewer.mention} → {posted.jump_url}**"
            if dm_failed:
                note += "\n*(Could not DM the submitter — their DMs may be closed.)*"
            await review_msg.edit(content=review_msg.content + note, view=None)
        except Exception:
            pass

        return True, f"Approved. {posted.jump_url}"

    async def reject_submission(
        self, guild_id: int, sub_id: int, *, reviewer: discord.Member, reason: str
    ) -> tuple[bool, str]:
        row = await self.bot.db.source_submission_get(guild_id, sub_id)
        if not row:
            return False, "Submission not found."

        ok = await self.bot.db.source_submission_try_reject(guild_id, sub_id, reviewer.id, reason)
        if not ok:
            return False, f"This submission is no longer pending (status: `{row.status}`)."

        dm_failed = False
        try:
            user = await self.bot.fetch_user(int(row.user_id))
            await user.send(f"Your source submission **{row.title}** was rejected.\nReason: {reason}")
        except Exception:
            dm_failed = True

        try:
            review_ch = await self.bot.fetch_channel(self.bot.cfg.REVIEW_CHANNEL_ID)
            review_msg = await review_ch.fetch_message(int(row.review_message_id))
            note = f"\n\n❌ **Rejected by {reviewer.mention}:** {reason}"
            if dm_failed:
                note += "\n*(Could not DM the submitter — their DMs may be closed.)*"
            await review_msg.edit(content=review_msg.content + note, view=None)
        except Exception:
            pass

        return True, "Rejected."

    async def edit_filename(
        self, guild_id: int, sub_id: int, *, author_part: str, short_title_part: str
    ) -> tuple[bool, str]:
        if not is_valid_filename_part(author_part) or not is_valid_filename_part(short_title_part):
            return False, "Invalid characters — only letters, digits and single hyphens are allowed (no spaces)."

        row = await self.bot.db.source_submission_get(guild_id, sub_id)
        if not row:
            return False, "Submission not found."
        if row.status != "pending":
            return False, f"This submission is no longer pending (status: `{row.status}`)."

        await self.bot.db.source_submission_update_filename(
            guild_id, sub_id, filename_author=author_part, filename_short_title=short_title_part
        )

        try:
            review_ch = await self.bot.fetch_channel(self.bot.cfg.REVIEW_CHANNEL_ID)
            review_msg = await review_ch.fetch_message(int(row.review_message_id))
            new_text = self._render_preview(row, filename_author=author_part, filename_short_title=short_title_part)
            await review_msg.edit(content=new_text)
        except Exception:
            pass

        return True, "Filename updated."


# Must run at import time, before setup(bot) -> add_cog() -> tree.add_command() reads
# the command's _guild_ids — mutating it after add_cog() would be too late.
if _GUILD_ID:
    app_commands.guilds(_GUILD_ID)(BookSourcesCog.submit)
else:
    log.warning("Book sources: GUILD_ID not resolvable at import time — /submit will register globally.")


async def setup(bot: commands.Bot):
    await bot.add_cog(BookSourcesCog(bot))
