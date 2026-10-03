from __future__ import annotations

import discord

def shorten_title(text: str, max_len: int = 60) -> str:
    t = " ".join(text.strip().split())
    if len(t) <= max_len:
        return t
    return t[: max_len - 1] + "…"

def status_label(status: str) -> str:
    mapping = {
        "queued": "Queued",
        "pending": "Pending",
        "claimed": "Claimed",
        "needs_context": "Needs Context",
        "answered": "Answered",
        "closed": "Closed",
        "denied": "Denied",
        "cancelled": "Cancelled",
    }
    return mapping.get(status, status)

def build_forward_embed(
    *,
    qid: int,
    question_text: str,
    author: discord.abc.User,
    origin_jump_url: str,
    tag: str | None,
    era: str | None,
    status: str,
    claimed_by_text: str | None = None,   # <-- NEW
) -> discord.Embed:
    e = discord.Embed(title=f"Historians of the House Team — Question #{qid}", description=question_text)
    e.add_field(name="Author", value=f"{author.mention}\n`{author.id}`", inline=True)
    e.add_field(name="Origin", value=f"[Jump to submission]({origin_jump_url})", inline=True)
    e.add_field(name="Tag / Era", value=f"{tag or '—'} / {era or '—'}", inline=False)

    # Status formatting
    status_txt = status_label(status)
    if status == "claimed" and claimed_by_text:
        status_txt = f"Claimed {claimed_by_text}"

    e.add_field(name="Status", value=f"**{status_txt}**", inline=True)
    e.set_footer(text="Use the buttons below to manage this request.")
    return e


def build_origin_embed(
    *,
    qid: int,
    question_text: str,
    tag: str | None,
    era: str | None,
    asker: discord.abc.User,
) -> discord.Embed:
    e = discord.Embed(title=f"Question #{qid}", description=question_text)
    e.add_field(name="Asked by", value=asker.mention, inline=True)
    e.add_field(name="Tag / Era", value=f"{tag or '—'} / {era or '—'}", inline=True)
    e.set_footer(text="A verified historian may answer in this thread.")
    return e

def build_answer_embed(
    *,
    qid: int,
    answer_text: str,
    answered_by: discord.abc.User,
) -> discord.Embed:
    e = discord.Embed(title=f"Answer — Question #{qid}", description=answer_text)
    e.add_field(name="Answered by", value=f"{answered_by.mention}\n`{answered_by.id}`", inline=False)
    e.set_footer(text="Historians of the House Team")
    return e


def build_watch_reminder_embed(question_text: str, jump_url: str) -> discord.Embed:
    e = discord.Embed(
        title="Still need help?",
        description="Your question hasn't received an answer yet. Would you like to ask the Historians of the House?",
        color=discord.Color.gold(),
    )
    e.add_field(name="Your question", value=shorten_title(question_text, 200), inline=False)
    e.set_footer(text="Only you (or a moderator) can use the button below.")
    return e


def build_watch_dm_embed(question_text: str, channel_mention: str, jump_url: str) -> discord.Embed:
    e = discord.Embed(
        title="Still need help?",
        description=(
            f"Your question in {channel_mention} hasn't received an answer yet.\n"
            "Would you like to notify the Historians of the House?"
        ),
        color=discord.Color.gold(),
    )
    e.add_field(name="Your question", value=shorten_title(question_text, 200), inline=False)
    e.add_field(name="Jump to your question", value=f"[Click here]({jump_url})", inline=False)
    return e


def build_topic_of_day_embed(topic: dict, date_key: str) -> discord.Embed:
    e = discord.Embed(
        title=f"🗓️ Topic of the Day — {date_key}",
        description=topic.get("prompt", ""),
        color=discord.Color.teal(),
    )
    meta = []
    era = topic.get("era")
    tag = topic.get("tag")
    if era:
        meta.append(str(era).replace("_", " ").title())
    if tag:
        meta.append(str(tag).title())
    if meta:
        e.add_field(name="Theme", value=" • ".join(meta), inline=False)
    e.set_footer(text="Share your take below — there's no right answer, just good discussion.")
    return e


def build_historian_request_embed(
    *,
    question_text: str,
    asker: discord.abc.User | None,
    jump_url: str,
    resolved_by_text: str | None = None,
) -> discord.Embed:
    asker_text = asker.mention if asker else "A community member"
    e = discord.Embed(
        title="📚 Historian Request",
        description=(
            f"{asker_text} is looking for help with the following historical question:\n\n"
            f"> {shorten_title(question_text, 500)}"
        ),
        color=discord.Color.green() if resolved_by_text else discord.Color.blurple(),
    )
    e.add_field(name="Jump to original message", value=f"[Click here]({jump_url})", inline=False)
    if resolved_by_text:
        e.add_field(name="Status", value=f"✅ Resolved by {resolved_by_text}", inline=False)
        e.set_footer(text="Historians of the House — this request has been resolved.")
    else:
        e.set_footer(text="Historians of the House — reply here or under the original question to respond.")
    return e
