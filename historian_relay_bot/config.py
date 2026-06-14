from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional
import datetime


from dotenv import load_dotenv

load_dotenv()

def _get_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "y", "on")

def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default

def _get_csv_ints(name: str, default: List[int]) -> List[int]:
    raw = os.getenv(name)
    if not raw:
        return default
    out: List[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(part))
        except ValueError:
            pass
    return out or default

def _get_optional_int(name: str) -> Optional[int]:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return None
    try:
        return int(raw)
    except ValueError:
        return None
    
def _get_int_list(key: str, default=None):
    """Read a CSV/space-separated list of ints from env or config.json."""
    if default is None:
        default = []

    raw = os.getenv(key)
    if raw is None and "data" in globals() and isinstance(data, dict):
        raw = data.get(key)

    if raw is None:
        return default

    # allow list already
    if isinstance(raw, list):
        out = []
        for v in raw:
            try:
                out.append(int(v))
            except Exception:
                pass
        return out

    # string: "123,456 789"
    s = str(raw).strip()
    if not s:
        return default

    parts = [p.strip() for p in s.replace(" ", ",").split(",") if p.strip()]
    out = []
    for p in parts:
        try:
            out.append(int(p))
        except Exception:
            pass
    return out

@dataclass(slots=True)
class BotConfig:
    DISCORD_TOKEN: str

    # Channels / roles
    SUBMISSION_CHANNEL_IDS: List[int]
    HISTORIANS_CHANNEL_ID: int
    QUEUE_CHANNEL_ID: Optional[int]
    VERIFIED_HISTORIAN_ROLE_ID: int
    MOD_ROLE_ID: int

    LOG_CHANNEL_ID: Optional[int] = None

    # Feature flags
    APPROVAL_MODE: bool = False
    THREADS_ENABLED: bool = True

    # Validation
    MIN_QUESTION_LENGTH: int = 40

    # Anti-spam
    COOLDOWN_MINUTES: int = 30
    MAX_PER_DAY: int = 5
    REQUIRE_ACCOUNT_AGE_DAYS: int = 0  # 0 = off
    PING_USER_ON_PUBLISH: bool = True  # ping in thread/origin message

    # Behavior
    ALLOW_PUBLISH_WITHOUT_CLAIM: bool = False

    # Keywords
    REGION_KEYWORDS: List[str] = field(default_factory=lambda: [
        "rome", "roman", "china", "europe", "ottoman", "byzantine", "persia", "egypt",
        "india", "mughal", "mongol", "america", "mesopotamia", "greece", "britain",
        "france", "germany", "spain", "austria", "russia", "japan", "korea"
    ])

    DATABASE_PATH: str = "historian_relay.sqlite3"

    # --------------------
    # Guess the Year
    # --------------------
    GUESSYEAR_ENABLED: bool = True
    GUESSYEAR_ALLOWED_CHANNEL_IDS: List[int] = field(default_factory=list)  # empty = allow all
    GUESSYEAR_ROUND_SECONDS: int = 30
    GUESSYEAR_COOLDOWN_SECONDS: int = 5
    GUESSYEAR_MIN_YEAR: int = 1
    GUESSYEAR_MAX_YEAR: int = 0  # 0 = current year at runtime
    GUESSYEAR_HINTS_ENABLED: bool = True
    GUESSYEAR_MAX_HINTS: int = 3
    GUESSYEAR_GUESS_POLICY: str = "first"  # "first" or "latest"
    GUESSYEAR_DAILY_CHANNEL_ID: Optional[int] = None
    GUESSYEAR_DAILY_HOUR_UTC: int = 12


def load_config() -> BotConfig:
    """
    Loads from env first. If CONFIG_JSON is set, it can override/define values.
    """
    data: dict = {}
    cfg_path = os.getenv("CONFIG_JSON")
    if cfg_path:
        p = Path(cfg_path)
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))

    def env_or_json(key: str, default=None):
        if os.getenv(key) is not None:
            return os.getenv(key)
        return data.get(key, default)

    token = env_or_json("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN missing.")

    submission_ids = _get_csv_ints("SUBMISSION_CHANNEL_IDS", data.get("SUBMISSION_CHANNEL_IDS", []))
    if not submission_ids:
        raise RuntimeError("SUBMISSION_CHANNEL_IDS missing/empty.")

    historians_ch = env_or_json("HISTORIANS_CHANNEL_ID")
    if not historians_ch:
        raise RuntimeError("HISTORIANS_CHANNEL_ID missing.")

    verified_role = env_or_json("VERIFIED_HISTORIAN_ROLE_ID")
    mod_role = env_or_json("MOD_ROLE_ID")
    if not verified_role or not mod_role:
        raise RuntimeError("VERIFIED_HISTORIAN_ROLE_ID and MOD_ROLE_ID required.")

    return BotConfig(
        DISCORD_TOKEN=str(token),
        SUBMISSION_CHANNEL_IDS=submission_ids,
        HISTORIANS_CHANNEL_ID=int(historians_ch),
        QUEUE_CHANNEL_ID=_get_optional_int("QUEUE_CHANNEL_ID") or (int(data["QUEUE_CHANNEL_ID"]) if data.get("QUEUE_CHANNEL_ID") else None),
        VERIFIED_HISTORIAN_ROLE_ID=int(verified_role),
        MOD_ROLE_ID=int(mod_role),
        LOG_CHANNEL_ID=_get_optional_int("LOG_CHANNEL_ID") or (int(data["LOG_CHANNEL_ID"]) if data.get("LOG_CHANNEL_ID") else None),
        APPROVAL_MODE=_get_bool("APPROVAL_MODE", bool(data.get("APPROVAL_MODE", False))),
        THREADS_ENABLED=_get_bool("THREADS_ENABLED", bool(data.get("THREADS_ENABLED", True))),
        MIN_QUESTION_LENGTH=_get_int("MIN_QUESTION_LENGTH", int(data.get("MIN_QUESTION_LENGTH", 40))),
        COOLDOWN_MINUTES=_get_int("COOLDOWN_MINUTES", int(data.get("COOLDOWN_MINUTES", 30))),
        MAX_PER_DAY=_get_int("MAX_PER_DAY", int(data.get("MAX_PER_DAY", 5))),
        REQUIRE_ACCOUNT_AGE_DAYS=_get_int("REQUIRE_ACCOUNT_AGE_DAYS", int(data.get("REQUIRE_ACCOUNT_AGE_DAYS", 0))),
        PING_USER_ON_PUBLISH=_get_bool("PING_USER_ON_PUBLISH", bool(data.get("PING_USER_ON_PUBLISH", True))),
        ALLOW_PUBLISH_WITHOUT_CLAIM=_get_bool("ALLOW_PUBLISH_WITHOUT_CLAIM", bool(data.get("ALLOW_PUBLISH_WITHOUT_CLAIM", False))),
        REGION_KEYWORDS=list(data.get("REGION_KEYWORDS", BotConfig.__dataclass_fields__["REGION_KEYWORDS"].default_factory())),
        DATABASE_PATH=str(env_or_json("DATABASE_PATH", data.get("DATABASE_PATH", "historian_relay.sqlite3"))),

        GUESSYEAR_ENABLED=_get_bool("GUESSYEAR_ENABLED", bool(data.get("GUESSYEAR_ENABLED", True))),
        GUESSYEAR_ALLOWED_CHANNEL_IDS=_get_int_list("GUESSYEAR_ALLOWED_CHANNEL_IDS") or list(map(int, data.get("GUESSYEAR_ALLOWED_CHANNEL_IDS", []) or [])),
        GUESSYEAR_ROUND_SECONDS=int(env_or_json("GUESSYEAR_ROUND_SECONDS") or data.get("GUESSYEAR_ROUND_SECONDS", 30)),
        GUESSYEAR_COOLDOWN_SECONDS=int(env_or_json("GUESSYEAR_COOLDOWN_SECONDS") or data.get("GUESSYEAR_COOLDOWN_SECONDS", 5)),
        GUESSYEAR_MIN_YEAR=int(env_or_json("GUESSYEAR_MIN_YEAR") or data.get("GUESSYEAR_MIN_YEAR", 1)),
        GUESSYEAR_MAX_YEAR=int(env_or_json("GUESSYEAR_MAX_YEAR") or data.get("GUESSYEAR_MAX_YEAR", 0)),
        GUESSYEAR_HINTS_ENABLED=_get_bool("GUESSYEAR_HINTS_ENABLED", bool(data.get("GUESSYEAR_HINTS_ENABLED", True))),
        GUESSYEAR_MAX_HINTS=int(env_or_json("GUESSYEAR_MAX_HINTS") or data.get("GUESSYEAR_MAX_HINTS", 3)),
        GUESSYEAR_GUESS_POLICY=str(env_or_json("GUESSYEAR_GUESS_POLICY") or data.get("GUESSYEAR_GUESS_POLICY", "first")),
        GUESSYEAR_DAILY_CHANNEL_ID=_get_optional_int("GUESSYEAR_DAILY_CHANNEL_ID") or (int(data["GUESSYEAR_DAILY_CHANNEL_ID"]) if data.get("GUESSYEAR_DAILY_CHANNEL_ID") else None),
        GUESSYEAR_DAILY_HOUR_UTC=_get_int("GUESSYEAR_DAILY_HOUR_UTC", int(data.get("GUESSYEAR_DAILY_HOUR_UTC", 12))),
    )
