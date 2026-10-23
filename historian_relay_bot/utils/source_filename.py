from __future__ import annotations

import re
import unicodedata

# "a/an/the" are only dropped when they lead the main title (see strip_leading_article);
# elsewhere (not first word) they're kept but lowercased, like the rest of this list.
MINOR_WORDS = {
    "a", "an", "the", "and", "but", "or", "nor",
    "of", "in", "on", "at", "to", "for", "by", "from", "with", "as",
}

_LEADING_ARTICLE_RE = re.compile(r"^(the|a|an)\s+", re.IGNORECASE)
_DISALLOWED_CHARS_RE = re.compile(r"[^A-Za-z0-9\s-]")
_FILENAME_PART_RE = re.compile(r"^[A-Za-z0-9]+(-[A-Za-z0-9]+)*$")


def strip_diacritics(text: str) -> str:
    """Transliterate to ASCII (e.g. Müller -> Muller) using stdlib unicodedata only."""
    nfkd = unicodedata.normalize("NFKD", text)
    without_marks = "".join(c for c in nfkd if not unicodedata.combining(c))
    return without_marks.encode("ascii", "ignore").decode("ascii")


def strip_leading_article(text: str) -> str:
    return _LEADING_ARTICLE_RE.sub("", text.strip(), count=1)


def main_title(title: str) -> str:
    """Text before the first ':', or the whole title if there's no colon."""
    return title.split(":", 1)[0].strip()


def parse_authors(raw: str) -> list[str]:
    """Split a ';'-separated author string into individual, stripped names."""
    return [p.strip() for p in raw.split(";") if p.strip()]


def build_surname(name: str) -> str:
    """Surname = last whitespace-separated token of a name, transliterated to ASCII."""
    tokens = name.strip().split()
    surname = tokens[-1] if tokens else name.strip()
    surname = strip_diacritics(surname)
    return re.sub(r"[^A-Za-z0-9-]", "", surname)


def build_author_part(authors: list[str], *, is_edited: bool) -> str:
    """1 author -> surname. 2 -> "Surname1-Surname2". 3+ -> "Surname1-etal". +"-ed" if edited volume."""
    surnames = [build_surname(a) for a in authors if a.strip()]
    if not surnames:
        return ""
    if len(surnames) == 1:
        base = surnames[0]
    elif len(surnames) == 2:
        base = f"{surnames[0]}-{surnames[1]}"
    else:
        base = f"{surnames[0]}-etal"
    return f"{base}-ed" if is_edited else base


def build_short_title(title: str) -> str:
    """Main title (before first ':'), leading article dropped, ASCII-folded, punctuation
    stripped, first 5 words joined with '-'. Minor words are lowercased unless first."""
    text = strip_leading_article(main_title(title))
    text = strip_diacritics(text)
    text = _DISALLOWED_CHARS_RE.sub("", text)
    tokens = [t for t in text.split() if t][:5]

    out = []
    for i, tok in enumerate(tokens):
        if i != 0 and tok.lower() in MINOR_WORDS:
            out.append(tok.lower())
        else:
            out.append(tok)
    return "-".join(out)


def build_filename(*, prefix: str, topic_code: str, author_part: str, year: int, short_title_part: str, ext: str) -> str:
    return f"{prefix}{topic_code}_{author_part}_{year}_{short_title_part}.{ext.lstrip('.')}"


def is_valid_filename_part(s: str) -> bool:
    """Same charset the automatic rule produces: letters, digits, single internal hyphens."""
    return bool(s) and bool(_FILENAME_PART_RE.match(s))


def normalize_for_dup(text: str) -> str:
    """Lowercase, ASCII-fold, strip punctuation/leading article, collapse whitespace —
    used to compare submissions for the duplicate-title check."""
    t = strip_leading_article(strip_diacritics(text).lower())
    t = re.sub(r"[^a-z0-9\s]", "", t)
    return " ".join(t.split())
