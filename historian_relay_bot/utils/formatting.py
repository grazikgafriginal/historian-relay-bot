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
    e = discord.Embed(title=f"Historian's of the House Relay — Question #{qid}", description=question_text)
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
    e.set_footer(text="Historian's of the House Relay")
    return e
