from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Iterable, Optional

YEAR_RE = re.compile(r"\b(1[0-9]{3}|20[0-9]{2})\b")
CENTURY_RE = re.compile(r"\b\d{1,2}(st|nd|rd|th)\s+century\b", re.IGNORECASE)
URL_RE = re.compile(r"https?://\S+")

# Words/contractions that mark a sentence as actually interrogative, as opposed
# to a declarative statement (an offer, a callout, banter) that merely ends in
# "?". Checked against the text with apostrophes stripped, so both "doesn't"
# and "doesnt" are covered by the single "doesnt" entry.
_QUESTION_WORDS = (
    "why", "what", "whats", "when", "whens", "where", "wheres",
    "who", "whos", "whom", "whose", "which", "how", "hows",
    "is", "isnt", "are", "arent", "was", "wasnt", "were", "werent",
    "do", "dont", "does", "doesnt", "did", "didnt",
    "can", "cant", "could", "couldnt",
    "would", "wouldnt", "should", "shouldnt",
    "will", "wont", "have", "havent", "has", "hasnt", "had", "hadnt",
)
QUESTION_WORD_RE = re.compile(r"\b(" + "|".join(_QUESTION_WORDS) + r")\b", re.IGNORECASE)

# Each match is the text of one "?"-terminated clause (up to but not past any
# earlier ./!/?). Scoping the question-word search to just that clause (rather
# than the whole message) matters: in "Agrippa? ...I wouldn't count him...",
# the real question-ish word ("wouldn't") sits in a later clause that never
# gets a "?", so it must not count toward whether "Agrippa?" is a question.
QUESTION_CLAUSE_RE = re.compile(r"[^.!?]*\?")

@dataclass(slots=True)
class CheckResult:
    ok: bool
    reason: str = ""

def word_count(text: str) -> int:
    return len([w for w in re.split(r"\s+", text.strip()) if w])

def is_probable_question(text: str, min_words: int = 4) -> bool:
    """Lightweight, non-AI detector for "this message is probably a question".

    Used to decide whether a chat message in a watched channel should start
    an unanswered-question timer. Intentionally simple — no AI classification —
    but a bare "?" isn't enough on its own: "Wanna see my list of generals?"
    and "You know that painting?" both end in "?" without actually asking
    anything the bot should chase an answer for. So, in order:
      1. URLs are stripped before looking for "?" (a query string like
         "...image.jpg?utm_source=..." isn't a question mark).
      2. The remaining text must contain an actual interrogative word
         (why/what/how/is/does/can/...), not just end in "?".
      3. A minimum word count, so short reactions like "really?" don't count.
    """
    t = (text or "").strip()
    if not t:
        return False
    stripped = URL_RE.sub("", t)
    if "?" not in stripped:
        return False
    if t.startswith(("!", "/")):
        return False

    normalized = stripped.replace("'", "")
    question_clauses = QUESTION_CLAUSE_RE.findall(normalized)
    if not any(QUESTION_WORD_RE.search(c) for c in question_clauses):
        return False

    return word_count(t) >= max(1, min_words)

def quality_check(
    question: str,
    *,
    min_len: int,
    tag: Optional[str],
    era: Optional[str],
    region_keywords: Iterable[str],
) -> CheckResult:
    q = question.strip()
    if len(q) < min_len:
        return CheckResult(False, f"Your question is a bit short. Aim for **{min_len}+ characters** and include more detail/context.")

    wc = word_count(q)
    if wc < 10:
        return CheckResult(False, "Please add more detail: **at least 10 words** (who/what/where/when, and what you're trying to understand).")

    q_lower = q.lower()
    if q_lower.startswith("why") and wc < 15:
        return CheckResult(False, "‘Why’ questions need more context. Add **timeframe**, **place**, and what explanation you're looking for.")

    has_year = bool(YEAR_RE.search(q))
    has_century = bool(CENTURY_RE.search(q))
    has_tag_era = bool(tag) or bool(era)
    has_region = any(k.lower() in q_lower for k in region_keywords)

    if not (has_year or has_century or has_tag_era or has_region):
        return CheckResult(
            False,
            "Please include at least one anchor: a **year** (e.g., 1453), a **century** (e.g., 5th century), a **tag/era**, or a recognizable **region** (e.g., Rome/China/Ottoman)."
        )

    return CheckResult(True)

def next_utc_midnight_ts(now: Optional[int] = None) -> int:
    now_dt = datetime.now(timezone.utc) if now is None else datetime.fromtimestamp(now, tz=timezone.utc)
    tomorrow = (now_dt + timedelta(days=1)).date()
    midnight = datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=timezone.utc)
    return int(midnight.timestamp())

def spam_check(
    *,
    now_ts: int,
    last_asked_at: Optional[int],
    daily_count: int,
    daily_reset_at: int,
    cooldown_minutes: int,
    max_per_day: int,
) -> CheckResult:
    # reset daily counter at reset timestamp
    if now_ts >= daily_reset_at:
        daily_count = 0

    if last_asked_at is not None:
        cooldown_sec = cooldown_minutes * 60
        if now_ts - last_asked_at < cooldown_sec:
            remaining = cooldown_sec - (now_ts - last_asked_at)
            mins = max(1, int(remaining // 60))
            return CheckResult(False, f"You're on cooldown. Please wait about **{mins} more minutes** before submitting another question.")

    if daily_count >= max_per_day:
        return CheckResult(False, f"You've hit the daily cap (**{max_per_day}** questions/day). Please try again after the daily reset.")

    return CheckResult(True)

def account_age_check(user_created_at, require_days: int) -> CheckResult:
    if require_days <= 0:
        return CheckResult(True)

    now = datetime.now(timezone.utc)
    age = now - user_created_at
    if age < timedelta(days=require_days):
        return CheckResult(False, f"Account age requirement: your account must be **{require_days} days** old to use this command.")
    return CheckResult(True)
