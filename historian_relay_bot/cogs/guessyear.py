
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import random
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import discord
from discord.ext import commands

log = logging.getLogger(__name__)

YEAR_RE = re.compile(r"^\s*(-?\d{1,4})\s*$")


@dataclass(slots=True)
class RoundState:
    round_id: int
    guild_id: int
    channel_id: int
    event_id: str
    correct_year: int
    prompt: str
    hints: List[str]
    started_at: int
    ends_at: int
    hints_used: int


@dataclass(slots=True)
class BonusState:
    guild_id: int
    channel_id: int
    event_id: str
    source_round_id: int
    winner_user_id: int
    mode: str  # month / person
    prompt: str
    answers: List[str]
    started_at: int
    ends_at: int


@dataclass(slots=True)
class DuelChallengeState:
    guild_id: int
    channel_id: int
    challenger_user_id: int
    opponent_user_id: int
    total_questions: int
    created_at: int
    expires_at: int


@dataclass(slots=True)
class DuelQuestionResult:
    question_number: int
    event_id: str
    prompt: str
    correct_year: int
    guesses: Dict[int, int] = field(default_factory=dict)
    winner_user_id: Optional[int] = None
    winner_guess: Optional[int] = None
    winner_diff: Optional[int] = None


@dataclass(slots=True)
class DuelState:
    guild_id: int
    channel_id: int
    host_channel_id: int
    challenger_user_id: int
    opponent_user_id: int
    total_questions: int
    current_question: int
    event_id: str
    correct_year: int
    prompt: str
    started_at: int
    ends_at: int
    duel_thread_created: bool = False
    guesses: Dict[int, Tuple[int, int]] = field(default_factory=dict)  # user_id -> (guess_year, guessed_at)
    scores: Dict[int, int] = field(default_factory=dict)
    history: List[DuelQuestionResult] = field(default_factory=list)


@dataclass(slots=True)
class LearnSessionState:
    guild_id: int
    channel_id: int
    host_channel_id: int
    owner_user_id: int
    current_event_id: str
    correct_year: int
    prompt: str
    category_keys: List[str]
    started_at: int
    questions_answered: int = 0
    exact_hits: int = 0
    current_hints_used: int = 0
    awaiting_answer: bool = True
    used_event_ids: List[str] = field(default_factory=list)


CATEGORY_DEFINITIONS: List[Dict[str, Any]] = [
    {"key": "ancient", "label": "Ancient", "emoji": "🏺", "description": "Rome, Greece, early empires", "tags": {"Ancient", "Rome"}},
    {"key": "medieval", "label": "Medieval", "emoji": "⚔️", "description": "Middle Ages and kingdoms", "tags": {"Medieval"}},
    {"key": "europe", "label": "Europe", "emoji": "🇪🇺", "description": "European history", "tags": {"Europe", "England", "Britain", "France", "Netherlands", "Portugal", "Spain", "Italy", "Poland", "Byzantium", "Prussia", "Russia", "Ireland"}},
    {"key": "americas", "label": "Americas", "emoji": "🌎", "description": "North and South America", "tags": {"Americas", "America", "North America", "Caribbean", "California", "Canada", "Mexico", "Hawaii", "Inca"}},
    {"key": "asia", "label": "Asia", "emoji": "🌏", "description": "Asian history", "tags": {"Asia", "China", "India", "Japan", "Korea", "Southeast Asia", "Samurai", "Dynasty", "Afghanistan", "Nepal"}},
    {"key": "africa", "label": "Africa", "emoji": "🌍", "description": "African history", "tags": {"Africa"}},
    {"key": "middle_east", "label": "Middle East", "emoji": "🕌", "description": "Middle Eastern history", "tags": {"Middle East", "Ottoman Empire", "Ottoman", "Arabia", "Iran"}},
    {"key": "war", "label": "War", "emoji": "🪖", "description": "Battles, conflicts, conquests", "tags": {"War", "Conflict", "Rebellion", "Naval", "Conquest", "Terrorism"}},
    {"key": "politics", "label": "Politics", "emoji": "🏛️", "description": "Leaders, states, power", "tags": {"Politics", "State Formation", "Monarchy", "Empire", "Constitution", "Rights", "Independence"}},
    {"key": "science", "label": "Science", "emoji": "🔬", "description": "Discoveries and research", "tags": {"Science", "Mathematics", "Biology", "Physics", "Medicine", "Discovery", "Engineering"}},
    {"key": "exploration", "label": "Exploration", "emoji": "🧭", "description": "Voyages and expansion", "tags": {"Exploration", "Navigation", "Migration"}},
    {"key": "economy", "label": "Economy", "emoji": "💰", "description": "Trade, money, industry", "tags": {"Economy", "Economics", "Industry", "Trade", "Taxation"}},
    {"key": "religion", "label": "Religion", "emoji": "⛪", "description": "Faith and religious change", "tags": {"Religion"}},
    {"key": "revolution", "label": "Revolution", "emoji": "🔥", "description": "Revolution and protest", "tags": {"Revolution", "Protest"}},
    {"key": "law", "label": "Law", "emoji": "📜", "description": "Laws and legal change", "tags": {"Law", "Rights", "Constitution"}},
    {"key": "disaster", "label": "Disaster", "emoji": "🌋", "description": "Disasters and crises", "tags": {"Disaster"}},
    {"key": "cold_war", "label": "Cold War", "emoji": "🧊", "description": "Superpower rivalry", "tags": {"Cold War"}},
    {"key": "technology", "label": "Technology", "emoji": "⚙️", "description": "Inventions and tech change", "tags": {"Technology", "Communication", "Publishing", "Invention"}},
    {"key": "space", "label": "Space", "emoji": "🚀", "description": "Spaceflight and astronomy", "tags": {"Space"}},
    {"key": "civil_rights", "label": "Civil Rights", "emoji": "✊", "description": "Rights movements", "tags": {"Civil Rights", "Rights"}},
    {"key": "diplomacy", "label": "Diplomacy", "emoji": "🤝", "description": "Treaties and negotiations", "tags": {"Diplomacy"}},
    {"key": "colonial", "label": "Colonial", "emoji": "🚢", "description": "Empires and colonial rule", "tags": {"Colonial", "Colonialism", "Indigenous"}},
    {"key": "culture", "label": "Culture", "emoji": "🎭", "description": "Culture, sports, ideas", "tags": {"Culture", "Sports", "Architecture", "Cities", "Ideas", "Memorial"}},
    {"key": "health", "label": "Health", "emoji": "🩺", "description": "Disease and public health", "tags": {"Health", "Medicine"}},
]

CATEGORY_LOOKUP: Dict[str, Dict[str, Any]] = {item["key"]: item for item in CATEGORY_DEFINITIONS}


class CategorySelect(discord.ui.Select):
    def __init__(self, cog: "GuessYearCog", owner_id: int, guild_id: int, channel_id: int):
        self.cog = cog
        self.owner_id = owner_id
        self.guild_id = guild_id
        self.channel_id = channel_id

        current = set(cog._channel_categories.get((guild_id, channel_id), []))
        options = [
            discord.SelectOption(
                label=item["label"],
                value=item["key"],
                description=item["description"],
                emoji=item["emoji"],
                default=item["key"] in current,
            )
            for item in CATEGORY_DEFINITIONS
        ]
        super().__init__(
            placeholder="Choose one or more categories for this channel",
            min_values=1,
            max_values=len(options),
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Only the person who opened this menu can change the categories.", ephemeral=True)
            return

        selected = list(self.values)
        matched = self.cog._count_events_for_categories(selected)
        if matched <= 0:
            await interaction.response.send_message("Those categories do not match any events. Try a broader selection.", ephemeral=True)
            return

        self.cog._channel_categories[(self.guild_id, self.channel_id)] = selected
        for option in self.options:
            option.default = option.value in set(selected)

        embed = self.cog._build_categories_embed(self.guild_id, self.channel_id, interaction.user)
        await interaction.response.edit_message(embed=embed, view=self.view)


class CategoryResetButton(discord.ui.Button):
    def __init__(self, cog: "GuessYearCog", owner_id: int, guild_id: int, channel_id: int):
        super().__init__(label="Reset to all", style=discord.ButtonStyle.secondary)
        self.cog = cog
        self.owner_id = owner_id
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Only the person who opened this menu can change the categories.", ephemeral=True)
            return

        self.cog._channel_categories.pop((self.guild_id, self.channel_id), None)
        view = GuessYearCategoriesView(self.cog, self.owner_id, self.guild_id, self.channel_id)
        view.message = interaction.message
        embed = self.cog._build_categories_embed(self.guild_id, self.channel_id, interaction.user)
        await interaction.response.edit_message(embed=embed, view=view)


class GuessYearCategoriesView(discord.ui.View):
    def __init__(self, cog: "GuessYearCog", owner_id: int, guild_id: int, channel_id: int):
        super().__init__(timeout=180)
        self.cog = cog
        self.owner_id = owner_id
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.message: Optional[discord.Message] = None

        self.add_item(CategorySelect(cog, owner_id, guild_id, channel_id))
        self.add_item(CategoryResetButton(cog, owner_id, guild_id, channel_id))

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass


class DuelChallengeView(discord.ui.View):
    def __init__(self, cog: "GuessYearCog", state: DuelChallengeState):
        super().__init__(timeout=max(1, state.expires_at - int(time.time())))
        self.cog = cog
        self.state = state
        self.message: Optional[discord.Message] = None

    async def on_timeout(self) -> None:
        key = (self.state.guild_id, self.state.channel_id)
        current = self.cog._duel_challenges.get(key)
        if not current or current.opponent_user_id != self.state.opponent_user_id or current.challenger_user_id != self.state.challenger_user_id:
            return

        self.cog._duel_challenges.pop(key, None)
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                embed = self.message.embeds[0] if self.message.embeds else None
                if embed:
                    embed = embed.copy()
                    embed.color = discord.Color.dark_grey()
                    embed.set_footer(text="Challenge expired.")
                    await self.message.edit(embed=embed, view=self)
                else:
                    await self.message.edit(view=self)
            except Exception:
                pass

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.state.opponent_user_id:
            await interaction.response.send_message("Only the challenged player can accept this duel.", ephemeral=True)
            return
        await self.cog._accept_duel(interaction, self.state, self)

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id not in (self.state.opponent_user_id, self.state.challenger_user_id):
            await interaction.response.send_message("Only the challenger or challenged player can decline this duel.", ephemeral=True)
            return
        await self.cog._decline_duel(interaction, self.state, self)


class DuelGuessModal(discord.ui.Modal, title="Submit hidden duel guess"):
    guess = discord.ui.TextInput(
        label="Year",
        placeholder="Enter a year like 1789",
        min_length=1,
        max_length=4,
        required=True,
    )

    def __init__(self, cog: "GuessYearCog", state: DuelState):
        super().__init__(timeout=120)
        self.cog = cog
        self.state = state

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog._submit_duel_guess(interaction, self.state, str(self.guess))


class DuelRoundView(discord.ui.View):
    def __init__(self, cog: "GuessYearCog", state: DuelState):
        super().__init__(timeout=None)
        self.cog = cog
        self.state = state
        self.message: Optional[discord.Message] = None

    @discord.ui.button(label="Submit hidden guess", style=discord.ButtonStyle.primary, emoji="🕵️")
    async def submit_hidden_guess(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id not in (self.state.challenger_user_id, self.state.opponent_user_id):
            await interaction.response.send_message("Only the two duel participants can submit a hidden guess.", ephemeral=True)
            return

        key = (self.state.guild_id, self.state.channel_id)
        active = self.cog._duel_active.get(key)
        if not active or active is not self.state:
            await interaction.response.send_message("This duel is no longer active.", ephemeral=True)
            return

        if interaction.user.id in active.guesses:
            await interaction.response.send_message("You have already locked in your hidden guess for this duel.", ephemeral=True)
            return

        await interaction.response.send_modal(DuelGuessModal(self.cog, active))


class ThreadClosePromptView(discord.ui.View):
    def __init__(self, cog: "GuessYearCog", state: DuelState):
        super().__init__(timeout=300)
        self.cog = cog
        self.state = state
        self.message: Optional[discord.Message] = None

    def _allowed(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id in (self.state.challenger_user_id, self.state.opponent_user_id):
            return True
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        return bool(member and self.cog._can_manage_rounds(member))

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass

    @discord.ui.button(label="Close thread", style=discord.ButtonStyle.danger, emoji="🧵")
    async def close_thread(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self._allowed(interaction):
            await interaction.response.send_message("Only the duel participants or a moderator can close this thread.", ephemeral=True)
            return

        channel = interaction.channel
        if not isinstance(channel, discord.Thread):
            await interaction.response.send_message("This control only works inside the duel thread.", ephemeral=True)
            return

        for child in self.children:
            child.disabled = True

        embed = discord.Embed(
            title="🧵 Duel Thread Closed",
            description=f"Archived by {interaction.user.mention}.",
            color=discord.Color.dark_grey(),
        )
        await interaction.response.edit_message(embed=embed, view=self)

        try:
            await channel.delete()
        except Exception:
            pass

    @discord.ui.button(label="Keep open", style=discord.ButtonStyle.secondary)
    async def keep_open(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self._allowed(interaction):
            await interaction.response.send_message("Only the duel participants or a moderator can keep this thread open.", ephemeral=True)
            return

        for child in self.children:
            child.disabled = True

        embed = discord.Embed(
            title="🧵 Duel Thread Kept Open",
            description=f"{interaction.user.mention} chose to keep this duel thread open.",
            color=discord.Color.blurple(),
        )
        await interaction.response.edit_message(embed=embed, view=self)


class LearnSessionView(discord.ui.View):
    def __init__(self, cog: "GuessYearCog", state: LearnSessionState):
        super().__init__(timeout=None)
        self.cog = cog
        self.state = state

    def _alive(self) -> bool:
        active = self.cog._learn_active.get((self.state.guild_id, self.state.channel_id))
        return active is self.state

    @discord.ui.button(label="Hint", style=discord.ButtonStyle.secondary, emoji="💡")
    async def hint(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self._alive():
            await interaction.response.send_message("This learning session is no longer active.", ephemeral=True)
            return
        await self.cog._learn_hint_interaction(interaction, self.state)

    @discord.ui.button(label="Next Question", style=discord.ButtonStyle.primary, emoji="➡️")
    async def next_question(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self._alive():
            await interaction.response.send_message("This learning session is no longer active.", ephemeral=True)
            return
        await self.cog._learn_next_interaction(interaction, self.state)

    @discord.ui.button(label="End Session", style=discord.ButtonStyle.danger, emoji="🛑")
    async def end_session(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self._alive():
            await interaction.response.send_message("This learning session is no longer active.", ephemeral=True)
            return
        await self.cog._end_learn_session_interaction(interaction, self.state)


class GuessYearCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

        self._active: Dict[Tuple[int, int], RoundState] = {}
        self._end_tasks: Dict[Tuple[int, int], asyncio.Task] = {}
        self._guess_cooldown: Dict[Tuple[int, int, int], int] = {}  # (guild, channel, user) -> last_ts

        self._events_by_id: Dict[str, Dict[str, Any]] = {}
        self._events: List[Dict[str, Any]] = []

        self._recent_finished: Dict[Tuple[int, int], Dict[str, Any]] = {}
        self._bonus_active: Dict[Tuple[int, int], BonusState] = {}
        self._channel_categories: Dict[Tuple[int, int], List[str]] = {}

        self._duel_challenges: Dict[Tuple[int, int], DuelChallengeState] = {}
        self._duel_challenge_views: Dict[Tuple[int, int], DuelChallengeView] = {}
        self._duel_active: Dict[Tuple[int, int], DuelState] = {}
        self._duel_round_views: Dict[Tuple[int, int], DuelRoundView] = {}
        self._duel_tasks: Dict[Tuple[int, int], asyncio.Task] = {}
        self._duel_finishing: set = set()

        self._learn_active: Dict[Tuple[int, int], LearnSessionState] = {}
        self._learn_owner_threads: Dict[Tuple[int, int], int] = {}
        self._learn_views: Dict[Tuple[int, int], LearnSessionView] = {}

        self._restore_started = False

        self._load_dataset()

    async def cog_load(self) -> None:
        # Restore active rounds once after the bot is ready.
        if not self._restore_started:
            self._restore_started = True
            asyncio.create_task(self._restore_after_ready())

    # ---------- dataset / restore ----------

    def _load_dataset(self) -> None:
        data_path = Path(__file__).resolve().parent.parent / "data" / "guessyear_events.json"
        if not data_path.exists():
            log.error("GuessYear dataset missing at %s", data_path)
            self._events = []
            self._events_by_id = {}
            return

        try:
            raw = json.loads(data_path.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                raise ValueError("dataset must be a list of events")
            self._events = [e for e in raw if isinstance(e, dict) and "id" in e and "year" in e and "prompt" in e]
            self._events_by_id = {str(e["id"]): e for e in self._events}
            log.info("Loaded GuessYear dataset: %d events", len(self._events))
        except Exception:
            log.exception("Failed to load GuessYear dataset")
            self._events = []
            self._events_by_id = {}

    async def _restore_after_ready(self) -> None:
        await self.bot.wait_until_ready()
        if not self.bot.cfg.GUESSYEAR_ENABLED:
            return

        now = int(time.time())
        try:
            rows = await self.bot.db.guessyear_list_active_rounds(now)
        except Exception:
            log.exception("Failed to restore GuessYear rounds")
            return

        restored = 0
        for r in rows:
            try:
                guild_id = int(r["guild_id"])
                channel_id = int(r["channel_id"])
                key = (guild_id, channel_id)

                evt = self._events_by_id.get(str(r["event_id"]))
                if not evt:
                    await self.bot.db.guessyear_mark_round_cancelled(int(r["round_id"]))
                    continue

                state = RoundState(
                    round_id=int(r["round_id"]),
                    guild_id=guild_id,
                    channel_id=channel_id,
                    event_id=str(r["event_id"]),
                    correct_year=int(r["correct_year"]),
                    prompt=str(evt["prompt"]),
                    hints=list(evt.get("hints", [])),
                    started_at=int(r["started_at"]),
                    ends_at=int(r["ends_at"]),
                    hints_used=int(r.get("hints_used", 0)),
                )

                self._active[key] = state
                self._schedule_end(state)
                restored += 1
            except Exception:
                log.exception("Failed restoring one GuessYear round")

        if restored:
            log.info("Restored %d GuessYear active rounds", restored)

    # ---------- helpers ----------

    def _is_allowed_channel(self, channel_id: int) -> bool:
        allowed = getattr(self.bot.cfg, "GUESSYEAR_ALLOWED_CHANNEL_IDS", [])
        return (not allowed) or (channel_id in allowed)

    def _resolve_max_year(self) -> int:
        mx = int(getattr(self.bot.cfg, "GUESSYEAR_MAX_YEAR", 0) or 0)
        return mx if mx > 0 else datetime.datetime.utcnow().year

    def _pick_event(self, guild_id: Optional[int] = None, channel_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
        pool = self._events
        if guild_id is not None and channel_id is not None:
            pool = self._events_for_channel(guild_id, channel_id)
        if not pool:
            return None
        return random.choice(pool)

    def _remaining(self, ends_at: int) -> int:
        return max(0, ends_at - int(time.time()))

    def _categories_for_channel(self, guild_id: int, channel_id: int) -> List[str]:
        raw = self._channel_categories.get((guild_id, channel_id), [])
        return [key for key in raw if key in CATEGORY_LOOKUP]

    def _events_for_channel(self, guild_id: int, channel_id: int) -> List[Dict[str, Any]]:
        selected = self._categories_for_channel(guild_id, channel_id)
        if not selected:
            return list(self._events)

        allowed_tags = set()
        for key in selected:
            allowed_tags.update(CATEGORY_LOOKUP[key]["tags"])

        return [
            evt for evt in self._events
            if allowed_tags.intersection({str(tag) for tag in evt.get("tags", [])})
        ]

    def _count_events_for_categories(self, category_keys: List[str]) -> int:
        selected = [key for key in category_keys if key in CATEGORY_LOOKUP]
        if not selected:
            return len(self._events)

        allowed_tags = set()
        for key in selected:
            allowed_tags.update(CATEGORY_LOOKUP[key]["tags"])

        return sum(1 for evt in self._events if allowed_tags.intersection({str(tag) for tag in evt.get("tags", [])}))

    def _format_category_list(self, category_keys: List[str]) -> str:
        selected = [key for key in category_keys if key in CATEGORY_LOOKUP]
        if not selected:
            return "All categories"
        return ", ".join(f"**{CATEGORY_LOOKUP[key]['label']}**" for key in selected)

    def _build_categories_embed(self, guild_id: int, channel_id: int, user: discord.abc.User) -> discord.Embed:
        selected = self._categories_for_channel(guild_id, channel_id)
        count = len(self._events_for_channel(guild_id, channel_id))
        embed = discord.Embed(
            title="🎛️ GuessYear Categories",
            description=(
                "Choose the categories this channel should use for future GuessYear rounds.\n"
                "Events match **any** selected category. Changing categories does not affect a round already in progress."
            ),
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Current filter", value=self._format_category_list(selected), inline=False)
        embed.add_field(name="Matching events", value=f"**{count}** available event(s)", inline=True)
        embed.add_field(name="Changed by", value=user.mention, inline=True)
        embed.set_footer(text="Use !categories reset to go back to all events.")
        return embed

    def _schedule_end(self, state: RoundState) -> None:
        key = (state.guild_id, state.channel_id)

        old = self._end_tasks.get(key)
        if old and not old.done():
            old.cancel()

        self._end_tasks[key] = asyncio.create_task(self._end_round_when_ready(state))

    async def _end_round_when_ready(self, state: RoundState) -> None:
        delay = max(0, state.ends_at - int(time.time()))
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return

        key = (state.guild_id, state.channel_id)
        current = self._active.get(key)
        if not current or current.round_id != state.round_id:
            return

        await self._end_round(state.guild_id, state.channel_id, forced=False)

    async def _ensure_state_loaded(self, guild_id: int, channel_id: int) -> Optional[RoundState]:
        key = (guild_id, channel_id)
        if key in self._active:
            return self._active[key]

        try:
            row = await self.bot.db.guessyear_get_active_round(guild_id, channel_id, int(time.time()))
        except Exception:
            return None

        if not row:
            return None

        evt = self._events_by_id.get(str(row["event_id"]))
        if not evt:
            await self.bot.db.guessyear_mark_round_cancelled(int(row["round_id"]))
            return None

        state = RoundState(
            round_id=int(row["round_id"]),
            guild_id=int(row["guild_id"]),
            channel_id=int(row["channel_id"]),
            event_id=str(row["event_id"]),
            correct_year=int(row["correct_year"]),
            prompt=str(evt["prompt"]),
            hints=list(evt.get("hints", [])),
            started_at=int(row["started_at"]),
            ends_at=int(row["ends_at"]),
            hints_used=int(row.get("hints_used", 0)),
        )

        self._active[key] = state
        self._schedule_end(state)
        return state

    def _cleanup_expired_bonus(self, guild_id: Optional[int] = None, channel_id: Optional[int] = None) -> None:
        now = int(time.time())
        for key, bonus in list(self._bonus_active.items()):
            if guild_id is not None and key[0] != guild_id:
                continue
            if channel_id is not None and key[1] != channel_id:
                continue
            if bonus.ends_at <= now:
                self._bonus_active.pop(key, None)

        max_age = 15 * 60
        for key, recent in list(self._recent_finished.items()):
            if guild_id is not None and key[0] != guild_id:
                continue
            if channel_id is not None and key[1] != channel_id:
                continue
            if int(recent.get("unlocked_at", 0)) + max_age <= now:
                self._recent_finished.pop(key, None)

    def _normalize_bonus_text(self, text: str) -> str:
        cleaned = re.sub(r"[^a-z0-9\s]+", " ", (text or "").strip().lower())
        return " ".join(cleaned.split())

    def _pick_bonus_definition(
        self,
        evt: Dict[str, Any],
        requested_mode: Optional[str] = None,
    ) -> Optional[Tuple[str, Dict[str, Any]]]:
        bonus = evt.get("bonus")
        if not isinstance(bonus, dict):
            return None

        available: Dict[str, Dict[str, Any]] = {}
        for mode in ("month", "person"):
            data = bonus.get(mode)
            if isinstance(data, dict) and data.get("prompt") and isinstance(data.get("answers"), list) and data["answers"]:
                available[mode] = data

        if not available:
            return None

        if requested_mode:
            data = available.get(requested_mode)
            if not data:
                return None
            return requested_mode, data

        if len(available) == 1:
            mode, data = next(iter(available.items()))
            return mode, data

        return None

    def _bonus_modes_for_event(self, evt: Dict[str, Any]) -> List[str]:
        bonus = evt.get("bonus")
        if not isinstance(bonus, dict):
            return []

        modes: List[str] = []
        for mode in ("month", "person"):
            data = bonus.get(mode)
            if isinstance(data, dict) and data.get("prompt") and isinstance(data.get("answers"), list) and data["answers"]:
                modes.append(mode)
        return modes

    def _bonus_matches(self, answer: str, valid_answers: List[str]) -> bool:
        normalized = self._normalize_bonus_text(answer)
        valid = {self._normalize_bonus_text(a) for a in valid_answers}
        return normalized in valid

    def _format_member_label(self, guild: discord.Guild, uid: int, member: Optional[discord.Member]) -> str:
        mention = member.mention if member else f"<@{uid}>"
        raw_name = member.display_name if member else f"User {uid}"
        safe_name = discord.utils.escape_markdown(raw_name)
        return f"**{safe_name}** • {mention}"

    def _build_bonus_embed(self, bonus: BonusState, guild: discord.Guild, member: Optional[discord.Member]) -> discord.Embed:
        owner = self._format_member_label(guild, bonus.winner_user_id, member)
        embed = discord.Embed(
            title="🎁 GuessYear Bonus Round",
            description=bonus.prompt,
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Unlocked by", value=owner, inline=False)
        embed.add_field(name="Bonus type", value=bonus.mode.title(), inline=True)
        embed.add_field(name="Round", value=f"#{bonus.source_round_id}", inline=True)
        embed.add_field(name="How to answer", value="Use `!bonus <answer>`", inline=False)
        embed.set_footer(text=f"Time limit: {max(0, bonus.ends_at - int(time.time()))} seconds")
        return embed

    def _pick_learn_event(self, category_keys: List[str], used_event_ids: Optional[set[str]] = None) -> Optional[Dict[str, Any]]:
        if category_keys:
            allowed_tags = set()
            for key in category_keys:
                if key in CATEGORY_LOOKUP:
                    allowed_tags.update(CATEGORY_LOOKUP[key]["tags"])
            pool = [
                evt for evt in self._events
                if allowed_tags.intersection({str(tag) for tag in evt.get("tags", [])})
            ]
        else:
            pool = list(self._events)
        if not pool:
            return None
        used = used_event_ids or set()
        fresh = [evt for evt in pool if str(evt.get("id")) not in used]
        return random.choice(fresh or pool)

    def _learn_thread_name(self, member: discord.Member) -> str:
        base = re.sub(r"[^a-z0-9]+", "-", member.display_name.lower()).strip("-")[:20] or "learner"
        return f"learn-{base}-{int(time.time()) % 10000}"

    async def _create_learn_thread(
        self,
        ctx: commands.Context,
    ) -> discord.Thread:
        channel = ctx.channel
        guild = ctx.guild

        if not isinstance(channel, discord.TextChannel) or guild is None:
            raise RuntimeError("Learn mode can only be started from a normal server text channel.")

        owner = ctx.author if isinstance(ctx.author, discord.Member) else guild.get_member(ctx.author.id)
        if owner is None:
            try:
                owner = await guild.fetch_member(ctx.author.id)
            except Exception:
                owner = None

        if owner is None:
            raise RuntimeError("Could not resolve the learner for thread creation.")

        thread = await channel.create_thread(
            name=self._learn_thread_name(owner),
            type=discord.ChannelType.private_thread,
            auto_archive_duration=60,
            invitable=False,
        )

        try:
            await thread.add_user(owner)
        except Exception:
            pass

        return thread

    def _learn_feedback(self, diff: int) -> str:
        if diff == 0:
            return "Perfect — exact year. Great job."
        if diff <= 3:
            return "Very close. You clearly had the right time period in mind."
        if diff <= 10:
            return "Nice try. You were in the right neighborhood."
        if diff <= 25:
            return "Solid attempt. You were reasonably close for a learning round."
        return "Good effort. This one is worth reviewing again later."

    def _build_learn_intro_embed(self, member: discord.Member, state: LearnSessionState) -> discord.Embed:
        embed = discord.Embed(
            title="📚 GuessYear Learn Mode",
            description=(
                f"Welcome, {member.mention}. This private practice thread is for relaxed learning.\n\n"
                "Type a year like `1789` as a normal message to answer. Use the buttons below or the commands "
                "`!learnhint`, `!learnnext`, and `!learnstop`."
            ),
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Categories", value=self._format_category_list(state.category_keys), inline=False)
        embed.add_field(name="Goal", value="Learn with less pressure — no public leaderboard, just feedback.", inline=False)
        return embed

    def _build_learn_question_embed(self, state: LearnSessionState, member: discord.Member) -> discord.Embed:
        embed = discord.Embed(
            title=f"📖 Practice Question #{state.questions_answered + 1}",
            description=state.prompt,
            color=discord.Color.teal(),
        )
        embed.add_field(name="Learner", value=member.mention, inline=True)
        embed.add_field(name="Categories", value=self._format_category_list(state.category_keys), inline=True)
        embed.add_field(name="How to answer", value="Type a year like `1914` in this thread.", inline=False)
        return embed

    def _build_learn_result_embed(self, state: LearnSessionState, guess_year: int, evt: Dict[str, Any], diff: int) -> discord.Embed:
        tags = [str(t) for t in evt.get("tags", [])[:5]]
        hints = [str(h) for h in evt.get("hints", [])[:2]]
        embed = discord.Embed(
            title="🧠 Practice Result",
            description=self._learn_feedback(diff),
            color=discord.Color.green() if diff == 0 else discord.Color.orange(),
        )
        embed.add_field(name="Your guess", value=f"**{guess_year}**", inline=True)
        embed.add_field(name="Correct year", value=f"**{state.correct_year}**", inline=True)
        embed.add_field(name="Distance", value=f"**{diff} year(s)**", inline=True)
        embed.add_field(name="What happened", value=state.prompt, inline=False)
        if hints:
            embed.add_field(name="Context clues", value=" • ".join(hints), inline=False)
        if tags:
            embed.add_field(name="Related tags", value=", ".join(tags), inline=False)
        embed.set_footer(text="Use Next Question when you are ready for another one.")
        return embed

    async def _post_learn_question(self, channel: discord.abc.Messageable, state: LearnSessionState) -> None:
        guild = self.bot.get_guild(state.guild_id)
        if guild is None:
            return

        member = guild.get_member(state.owner_user_id)
        if member is None:
            try:
                member = await guild.fetch_member(state.owner_user_id)
            except Exception:
                member = None

        if member is None:
            # Fallback: still post the question instead of silently failing
            embed = discord.Embed(
                title=f"📖 Practice Question #{state.questions_answered + 1}",
                description=state.prompt,
                color=discord.Color.teal(),
            )
            embed.add_field(name="Learner", value=f"<@{state.owner_user_id}>", inline=True)
            embed.add_field(name="Categories", value=self._format_category_list(state.category_keys), inline=True)
            embed.add_field(name="How to answer", value="Type a year like `1914` in this thread.", inline=False)

            view = LearnSessionView(self, state)
            key = (state.guild_id, state.channel_id)
            self._learn_views[key] = view
            await channel.send(embed=embed, view=view)
            return

        view = LearnSessionView(self, state)
        key = (state.guild_id, state.channel_id)
        self._learn_views[key] = view
        await channel.send(embed=self._build_learn_question_embed(state, member), view=view)

    async def _learn_hint_interaction(self, interaction: discord.Interaction, state: LearnSessionState) -> None:
        if interaction.user.id != state.owner_user_id:
            await interaction.response.send_message("Only the learner who owns this session can request hints.", ephemeral=True)
            return
        if not state.awaiting_answer:
            await interaction.response.send_message("This question is already answered. Click Next Question for a new one.", ephemeral=True)
            return
        evt = self._events_by_id.get(state.current_event_id) or {}
        hints = list(evt.get("hints", []))
        if state.current_hints_used >= len(hints):
            await interaction.response.send_message("No more hints are available for this practice question.", ephemeral=True)
            return
        hint_text = str(hints[state.current_hints_used])
        state.current_hints_used += 1
        await interaction.response.send_message(f"💡 Hint {state.current_hints_used}/{len(hints)}: **{hint_text}**", ephemeral=False)

    async def _learn_next_interaction(self, interaction: discord.Interaction, state: LearnSessionState) -> None:
        if interaction.user.id != state.owner_user_id:
            await interaction.response.send_message("Only the learner who owns this session can start the next question.", ephemeral=True)
            return
        evt = self._pick_learn_event(state.category_keys, used_event_ids=set(state.used_event_ids))
        if evt is None:
            await interaction.response.send_message("No practice events are available for this category selection.", ephemeral=True)
            return
        state.current_event_id = str(evt["id"])
        state.correct_year = int(evt["year"])
        state.prompt = str(evt["prompt"])
        state.current_hints_used = 0
        state.awaiting_answer = True
        state.used_event_ids.append(state.current_event_id)
        await interaction.response.send_message("📖 New practice question posted below.", ephemeral=True)
        channel = interaction.channel
        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            await self._post_learn_question(channel, state)

    async def _end_learn_session_interaction(self, interaction: discord.Interaction, state: LearnSessionState) -> None:
        if interaction.user.id != state.owner_user_id and not (isinstance(interaction.user, discord.Member) and self._can_manage_rounds(interaction.user)):
            await interaction.response.send_message("Only the learner or a moderator can end this session.", ephemeral=True)
            return
        key = (state.guild_id, state.channel_id)
        self._learn_active.pop(key, None)
        self._learn_owner_threads.pop((state.guild_id, state.owner_user_id), None)
        self._learn_views.pop(key, None)
        embed = discord.Embed(
            title="📚 Learning Session Ended",
            description=(
                f"Questions completed: **{state.questions_answered}**\n"
                f"Exact guesses: **{state.exact_hits}**"
            ),
            color=discord.Color.dark_grey(),
        )
        await interaction.response.send_message(embed=embed)

    def _can_manage_rounds(self, member: discord.Member) -> bool:
        is_mod_role = any(r.id == self.bot.cfg.MOD_ROLE_ID for r in member.roles)
        has_perm = member.guild_permissions.manage_messages
        return is_mod_role or has_perm

    def _find_duel_for_context(self, guild_id: int, channel_id: int) -> Optional[DuelState]:
        direct = self._duel_active.get((guild_id, channel_id))
        if direct:
            return direct
        for duel in self._duel_active.values():
            if duel.guild_id == guild_id and duel.host_channel_id == channel_id:
                return duel
        return None

    def _find_duel_challenge_for_context(self, guild_id: int, channel_id: int) -> Optional[DuelChallengeState]:
        return self._duel_challenges.get((guild_id, channel_id))

    def _build_duel_thread_name(self, challenger: discord.Member, opponent: discord.Member) -> str:
        def slug(name: str) -> str:
            cleaned = re.sub(r"[^a-z0-9]+", "-", name.lower())
            return cleaned.strip("-")[:20] or "player"

        return f"duel-{slug(challenger.display_name)}-vs-{slug(opponent.display_name)}-{int(time.time()) % 10000}"

    async def _maybe_create_duel_thread(
        self,
        interaction: discord.Interaction,
        challenge: DuelChallengeState,
    ) -> tuple[discord.abc.Messageable, int, bool]:
        channel = interaction.channel
        if isinstance(channel, discord.Thread):
            return channel, channel.id, False

        if not isinstance(channel, discord.TextChannel) or interaction.message is None:
            raise RuntimeError("Duel threads can only be created from a server text channel message.")

        guild = interaction.guild
        challenger = guild.get_member(challenge.challenger_user_id) if guild else None
        opponent = guild.get_member(challenge.opponent_user_id) if guild else None

        # Fall back to fetch if not cached
        if guild:
            if challenger is None:
                try:
                    challenger = await guild.fetch_member(challenge.challenger_user_id)
                except Exception:
                    challenger = None
            if opponent is None:
                try:
                    opponent = await guild.fetch_member(challenge.opponent_user_id)
                except Exception:
                    opponent = None

        if challenger is None or opponent is None:
            raise RuntimeError("Could not resolve duel participants for thread creation.")

        thread = await interaction.message.create_thread(
            name=self._build_duel_thread_name(challenger, opponent),
            auto_archive_duration=60,
        )
        try:
            await thread.add_user(challenger)
        except Exception:
            pass
        try:
            await thread.add_user(opponent)
        except Exception:
            pass
        return thread, channel.id, True

    async def _prompt_thread_close(self, channel: discord.Thread, state: DuelState) -> None:
        view = ThreadClosePromptView(self, state)
        embed = discord.Embed(
            title="🧵 Close this duel thread?",
            description="The duel is over. Choose whether this thread should be archived now or left open.",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Options", value="**Close thread** archives it now. **Keep open** leaves it available.", inline=False)
        msg = await channel.send(embed=embed, view=view)
        view.message = msg

    def _duel_busy_message(self, guild_id: int, channel_id: int) -> Optional[str]:
        key = (guild_id, channel_id)

        challenge = self._duel_challenges.get(key)
        if challenge and challenge.expires_at > int(time.time()):
            return "A duel challenge is already pending in this channel."

        duel = self._find_duel_for_context(guild_id, channel_id)
        if duel and duel.ends_at > int(time.time()):
            rem = self._remaining(duel.ends_at)
            return f"A duel is already active here. **{rem}s** remaining."

        return None

    def _pick_duel_event(self, guild_id: int, channel_id: int, used_event_ids: Optional[set[str]] = None) -> Optional[Dict[str, Any]]:
        pool = self._events_for_channel(guild_id, channel_id)
        if not pool:
            return None
        used = used_event_ids or set()
        fresh = [evt for evt in pool if str(evt.get("id")) not in used]
        return random.choice(fresh or pool)

    def _build_duel_challenge_embed(self, guild: discord.Guild, state: DuelChallengeState) -> discord.Embed:
        challenger = guild.get_member(state.challenger_user_id)
        opponent = guild.get_member(state.opponent_user_id)
        categories = self._format_category_list(self._categories_for_channel(state.guild_id, state.channel_id))
        embed = discord.Embed(
            title="⚔️ GuessYear Duel Challenge",
            description=f"{challenger.mention if challenger else f'<@{state.challenger_user_id}>'} challenged {opponent.mention if opponent else f'<@{state.opponent_user_id}>'} to a hidden-guess duel.",
            color=discord.Color.orange(),
        )
        embed.add_field(name="Format", value="One hidden guess each per question. Guesses are revealed only when each question ends.", inline=False)
        embed.add_field(name="Questions", value=f"**{state.total_questions}**", inline=True)
        embed.add_field(name="Categories", value=categories, inline=False)
        embed.add_field(name="Accept by", value=f"<t:{state.expires_at}:R>", inline=True)
        embed.add_field(name="Per-question timer", value=f"**{self.bot.cfg.GUESSYEAR_ROUND_SECONDS}s**", inline=True)
        embed.set_footer(text="Only the challenged player can accept.")
        return embed

    def _build_duel_round_embed(self, guild: discord.Guild, state: DuelState) -> discord.Embed:
        challenger = guild.get_member(state.challenger_user_id)
        opponent = guild.get_member(state.opponent_user_id)

        def status(uid: int) -> str:
            return "✅ Locked in" if uid in state.guesses else "⌛ Waiting"

        categories = self._format_category_list(self._categories_for_channel(state.guild_id, state.channel_id))
        challenger_score = int(state.scores.get(state.challenger_user_id, 0))
        opponent_score = int(state.scores.get(state.opponent_user_id, 0))
        embed = discord.Embed(
            title=f"⚔️ GuessYear Duel • Question {state.current_question}/{state.total_questions}",
            description=state.prompt,
            color=discord.Color.red(),
        )
        embed.add_field(
            name="🎮Players",
            value=(
                f"{self._format_member_label(guild, state.challenger_user_id, challenger)} — {status(state.challenger_user_id)}\n"
                f"{self._format_member_label(guild, state.opponent_user_id, opponent)} — {status(state.opponent_user_id)}"
            ),
            inline=False,
        )
        embed.add_field(
            name="📊Score",
            value=(
                f"{self._format_member_label(guild, state.challenger_user_id, challenger)}: **{challenger_score}**\n"
                f"{self._format_member_label(guild, state.opponent_user_id, opponent)}: **{opponent_score}**"
            ),
            inline=False,
        )
        embed.add_field(name="Categories", value=categories, inline=False)
        embed.add_field(name="Locked in", value=f"**{len(state.guesses)}/2**", inline=True)
        embed.add_field(name="Time remaining", value=f"**{max(0, state.ends_at - int(time.time()))}s**", inline=True)
        embed.add_field(name="How to guess", value="Click **Submit hidden guess** below. Each player gets one hidden guess for this question.", inline=False)
        return embed

    def _build_duel_result_embed(
        self,
        guild: discord.Guild,
        state: DuelState,
        result: DuelQuestionResult,
        forced: bool,
    ) -> discord.Embed:
        challenger = guild.get_member(state.challenger_user_id)
        opponent = guild.get_member(state.opponent_user_id)

        def guess_line(uid: int, member: Optional[discord.Member]) -> str:
            if uid not in result.guesses:
                return f"{self._format_member_label(guild, uid, member)} — _No guess submitted_"
            guess_year = int(result.guesses[uid])
            diff = abs(guess_year - result.correct_year)
            perfect = " 🎯" if diff == 0 else ""
            return f"{self._format_member_label(guild, uid, member)} — **{guess_year}** (off by **{diff}**){perfect}"

        embed = discord.Embed(
            title=f"⚔️ Duel Question {result.question_number}/{state.total_questions} Result",
            description=result.prompt,
            color=discord.Color.gold(),
        )
        embed.add_field(name="Correct year", value=f"**{result.correct_year}**", inline=False)
        embed.add_field(name="Challenger", value=guess_line(state.challenger_user_id, challenger), inline=False)
        embed.add_field(name="Opponent", value=guess_line(state.opponent_user_id, opponent), inline=False)

        if result.winner_user_id is None:
            embed.add_field(name="🥇Winner", value="No valid guesses were submitted.", inline=False)
        else:
            winner_member = guild.get_member(result.winner_user_id)
            winner_text = self._format_member_label(guild, result.winner_user_id, winner_member)
            suffix = f" with **{result.winner_guess}** (off by **{result.winner_diff}**)"
            if result.winner_diff == 0:
                suffix += " 🎯"
            embed.add_field(name="🥇Winner", value=winner_text + suffix, inline=False)

        challenger_score = int(state.scores.get(state.challenger_user_id, 0))
        opponent_score = int(state.scores.get(state.opponent_user_id, 0))
        embed.add_field(
            name="Match score",
            value=(
                f"{self._format_member_label(guild, state.challenger_user_id, challenger)}: **{challenger_score}**\n"
                f"{self._format_member_label(guild, state.opponent_user_id, opponent)}: **{opponent_score}**"
            ),
            inline=False,
        )

        if forced:
            embed.set_footer(text="Duel was cancelled early.")
        elif result.question_number >= state.total_questions:
            embed.set_footer(text="Final question completed.")
        elif len(result.guesses) == 2:
            embed.set_footer(text="Both hidden guesses were submitted.")
        else:
            embed.set_footer(text="Timer expired before both hidden guesses were submitted.")

        return embed

    def _build_duel_match_result_embed(
        self,
        guild: discord.Guild,
        state: DuelState,
        forced: bool,
    ) -> discord.Embed:
        challenger = guild.get_member(state.challenger_user_id)
        opponent = guild.get_member(state.opponent_user_id)
        challenger_score = int(state.scores.get(state.challenger_user_id, 0))
        opponent_score = int(state.scores.get(state.opponent_user_id, 0))

        if challenger_score > opponent_score:
            overall = f"{self._format_member_label(guild, state.challenger_user_id, challenger)} wins the duel **{challenger_score}–{opponent_score}**."
        elif opponent_score > challenger_score:
            overall = f"{self._format_member_label(guild, state.opponent_user_id, opponent)} wins the duel **{opponent_score}–{challenger_score}**."
        else:
            overall = f"The duel ends in a **{challenger_score}–{opponent_score}** tie."

        embed = discord.Embed(
            title="⚔️ GuessYear Duel • Final Result",
            description=overall,
            color=discord.Color.gold(),
        )
        embed.add_field(
            name="Final score",
            value=(
                f"{self._format_member_label(guild, state.challenger_user_id, challenger)}: **{challenger_score}**\n"
                f"{self._format_member_label(guild, state.opponent_user_id, opponent)}: **{opponent_score}**"
            ),
            inline=False,
        )

        lines = []
        for result in state.history[-10:]:
            if result.winner_user_id is None:
                winner_text = "No winner"
            else:
                winner_member = guild.get_member(result.winner_user_id)
                winner_text = self._format_member_label(guild, result.winner_user_id, winner_member)
                if result.winner_diff == 0:
                    winner_text += " 🎯"
            lines.append(f"**Q{result.question_number}** — **{result.correct_year}** • {winner_text}")
        embed.add_field(name="Question summary", value="\n".join(lines) if lines else "No completed questions.", inline=False)

        if forced:
            embed.set_footer(text="Duel ended early.")
        else:
            embed.set_footer(text=f"Played {len(state.history)}/{state.total_questions} question(s).")
        return embed

    def _schedule_duel_end(self, state: DuelState) -> None:
        key = (state.guild_id, state.channel_id)
        old = self._duel_tasks.get(key)
        if old and not old.done():
            old.cancel()
        self._duel_tasks[key] = asyncio.create_task(self._end_duel_when_ready(state))

    async def _end_duel_when_ready(self, state: DuelState) -> None:
        delay = max(1, state.ends_at - int(time.time()))
        question_snapshot = state.current_question  # ADD
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            log.warning("DEBUG timer CANCELLED: q=%s", state.current_question)  # ADD
            return
        
        log.warning("DEBUG timer FIRED: q=%s snapshot=%s", state.current_question, question_snapshot)  # ADD

        key = (state.guild_id, state.channel_id)
        current = self._duel_active.get(key)
        if not current or current is not state:
            log.warning("DEBUG bailed: no current or identity mismatch")  # ADD
            return
        if state.current_question != question_snapshot:  # ADD
            return

        await self._finish_duel(state.guild_id, state.channel_id, forced=False)

    async def _replace_duel_round_message(
        self,
        guild: discord.Guild,
        channel: discord.abc.Messageable,
        state: DuelState,
    ) -> None:
        key = (state.guild_id, state.channel_id)
        old_view = self._duel_round_views.pop(key, None)
        if old_view and old_view.message is not None:
            for child in old_view.children:
                child.disabled = True
            try:
                await old_view.message.edit(view=old_view)
            except Exception:
                pass

        round_view = DuelRoundView(self, state)
        round_embed = self._build_duel_round_embed(guild, state)
        msg = await channel.send(embed=round_embed, view=round_view)
        round_view.message = msg
        self._duel_round_views[key] = round_view

    async def _refresh_duel_message(self, guild_id: int, channel_id: int) -> None:
        key = (guild_id, channel_id)
        state = self._duel_active.get(key)
        view = self._duel_round_views.get(key)
        if not state or not view or view.message is None:
            return
        try:
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                return
            embed = self._build_duel_round_embed(guild, state)
            await view.message.edit(embed=embed, view=view)
        except Exception:
            pass

    async def _accept_duel(
        self,
        interaction: discord.Interaction,
        challenge: DuelChallengeState,
        view: DuelChallengeView,
    ) -> None:
        key = (challenge.guild_id, challenge.channel_id)
        current = self._duel_challenges.get(key)
        if not current or current is not challenge:
            await interaction.response.send_message("This duel challenge is no longer active.", ephemeral=True)
            return

        guild = interaction.guild
        channel = interaction.channel
        if guild is None or not isinstance(channel, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message("This duel can only be accepted in a server text channel.", ephemeral=True)
            return

        state = await self._ensure_state_loaded(guild.id, channel.id)
        if state and state.ends_at > int(time.time()):
            self._duel_challenges.pop(key, None)
            self._duel_challenge_views.pop(key, None)
            await interaction.response.edit_message(
                embed=discord.Embed(
                    title="⚔️ GuessYear Duel Challenge",
                    description="This duel could not start because a normal GuessYear round is already active here.",
                    color=discord.Color.dark_red(),
                ),
                view=None,
            )
            return

        self._cleanup_expired_bonus(guild.id, channel.id)
        active_bonus = self._bonus_active.get(key)
        if active_bonus and active_bonus.ends_at > int(time.time()):
            self._duel_challenges.pop(key, None)
            self._duel_challenge_views.pop(key, None)
            await interaction.response.edit_message(
                embed=discord.Embed(
                    title="⚔️ GuessYear Duel Challenge",
                    description="This duel could not start because a bonus round is active in this channel.",
                    color=discord.Color.dark_red(),
                ),
                view=None,
            )
            return

        if key in self._duel_active:
            self._duel_challenges.pop(key, None)
            self._duel_challenge_views.pop(key, None)
            await interaction.response.send_message("A duel is already active in this channel.", ephemeral=True)
            return

        evt = self._pick_duel_event(guild.id, channel.id)
        if not evt:
            self._duel_challenges.pop(key, None)
            self._duel_challenge_views.pop(key, None)
            await interaction.response.edit_message(
                embed=discord.Embed(
                    title="⚔️ GuessYear Duel Challenge",
                    description="No GuessYear events match this channel's selected categories.",
                    color=discord.Color.dark_red(),
                ),
                view=None,
            )
            return

        min_year = int(self.bot.cfg.GUESSYEAR_MIN_YEAR)
        max_year = self._resolve_max_year()
        correct_year = int(evt["year"])
        if correct_year < min_year or correct_year > max_year:
            self._duel_challenges.pop(key, None)
            self._duel_challenge_views.pop(key, None)
            await interaction.response.edit_message(
                embed=discord.Embed(
                    title="⚔️ GuessYear Duel Challenge",
                    description="A dataset event had an out-of-range year. Please fix the dataset.",
                    color=discord.Color.dark_red(),
                ),
                view=None,
            )
            return

        try:
            duel_channel, host_channel_id, created_thread = await self._maybe_create_duel_thread(interaction, challenge)
        except Exception as e:
            log.exception("Failed to create duel thread")
            await interaction.response.send_message(
                f"I couldn't create the duel thread: `{type(e).__name__}: {e}`",
                ephemeral=True,
            )
            return

        now = int(time.time())
        duel_state = DuelState(
            guild_id=guild.id,
            channel_id=duel_channel.id,
            host_channel_id=host_channel_id,
            challenger_user_id=challenge.challenger_user_id,
            opponent_user_id=challenge.opponent_user_id,
            total_questions=int(challenge.total_questions),
            current_question=1,
            event_id=str(evt["id"]),
            correct_year=correct_year,
            prompt=str(evt["prompt"]),
            started_at=now,
            ends_at=now + int(self.bot.cfg.GUESSYEAR_ROUND_SECONDS),
            duel_thread_created=created_thread,
            scores={challenge.challenger_user_id: 0, challenge.opponent_user_id: 0},
        )

        self._duel_challenges.pop(key, None)
        self._duel_challenge_views.pop(key, None)
        self._duel_active[(guild.id, duel_channel.id)] = duel_state
        self._schedule_duel_end(duel_state)

        accepted_embed = self._build_duel_challenge_embed(guild, challenge)
        accepted_embed.color = discord.Color.green()
        if isinstance(duel_channel, discord.Thread):
            accepted_embed.add_field(name="Duel thread", value=duel_channel.mention, inline=False)
            accepted_embed.set_footer(text="Challenge accepted. Continue inside the duel thread.")
        else:
            accepted_embed.set_footer(text="Challenge accepted.")
        await interaction.response.edit_message(embed=accepted_embed, view=None)

        if isinstance(channel, discord.TextChannel) and isinstance(duel_channel, discord.Thread):
            await channel.send(
                f"⚔️ {duel_channel.mention} is ready for {guild.get_member(challenge.challenger_user_id).mention if guild.get_member(challenge.challenger_user_id) else f'<@{challenge.challenger_user_id}>'} "
                f"vs {guild.get_member(challenge.opponent_user_id).mention if guild.get_member(challenge.opponent_user_id) else f'<@{challenge.opponent_user_id}>'}."
            )

        await self._replace_duel_round_message(guild, duel_channel, duel_state)

    async def _decline_duel(
        self,
        interaction: discord.Interaction,
        challenge: DuelChallengeState,
        view: DuelChallengeView,
    ) -> None:
        key = (challenge.guild_id, challenge.channel_id)
        current = self._duel_challenges.get(key)
        if not current or current is not challenge:
            await interaction.response.send_message("This duel challenge is no longer active.", ephemeral=True)
            return

        self._duel_challenges.pop(key, None)
        self._duel_challenge_views.pop(key, None)

        guild = interaction.guild
        desc = "The duel challenge was declined."
        if guild:
            user = guild.get_member(interaction.user.id)
            if user:
                desc = f"{user.mention} declined the duel challenge."

        embed = discord.Embed(
            title="⚔️ GuessYear Duel Challenge",
            description=desc,
            color=discord.Color.dark_red(),
        )
        await interaction.response.edit_message(embed=embed, view=None)
        view.stop()

    async def _submit_duel_guess(
        self,
        interaction: discord.Interaction,
        state: DuelState,
        guess_text: str,
    ) -> None:
        key = (state.guild_id, state.channel_id)
        active = self._duel_active.get(key)
        if not active or active is not state:
            await interaction.response.send_message("This duel is no longer active.", ephemeral=True)
            return

        if interaction.user.id not in (active.challenger_user_id, active.opponent_user_id):
            await interaction.response.send_message("Only the duel participants can submit a hidden guess.", ephemeral=True)
            return

        if interaction.user.id in active.guesses:
            await interaction.response.send_message("You have already locked in your hidden guess.", ephemeral=True)
            return

        m = YEAR_RE.match(guess_text or "")
        if not m:
            await interaction.response.send_message("Please enter a valid year like `1789`.", ephemeral=True)
            return

        guess_year = int(m.group(1))
        min_year = int(self.bot.cfg.GUESSYEAR_MIN_YEAR)
        max_year = self._resolve_max_year()
        if guess_year < min_year or guess_year > max_year:
            await interaction.response.send_message(f"That year is out of range. Valid range: {min_year}–{max_year}.", ephemeral=True)
            return

        now = int(time.time())
        if active.ends_at <= now:
            await interaction.response.send_message("This duel has already ended.", ephemeral=True)
            return

        active.guesses[interaction.user.id] = (guess_year, now)
        await interaction.response.send_message(
            f"Your hidden guess **{guess_year}** has been locked in.",
            ephemeral=True,
        )

        await self._refresh_duel_message(active.guild_id, active.channel_id)

        if len(active.guesses) >= 2:
            await self._finish_duel(active.guild_id, active.channel_id, forced=False)

    async def _finish_duel(self, guild_id: int, channel_id: int, forced: bool) -> None:
        key = (guild_id, channel_id)
        
        # Guard against re-entrant calls (e.g. timer fires while guess submission is mid-await)
        if key in self._duel_finishing:
            log.warning("DEBUG _finish_duel: re-entrancy guard hit")  # ADD
            return
        self._duel_finishing.add(key)

        state = self._duel_active.get(key)
        if not state:
            log.warning("DEBUG _finish_duel: no state found")  # ADD
            self._duel_finishing.discard(key)
            return

        task = self._duel_tasks.pop(key, None)
        current_task = asyncio.current_task()
        if task and not task.done() and task is not current_task:  # ADD: task is not current_task
            task.cancel()

        try:
            guild = self.bot.get_guild(guild_id)
            log.warning("DEBUG guild=%s channel_obj=%s", guild, self.bot.get_channel(channel_id))  # ADD
            if guild is None:
                log.warning("DEBUG _finish_duel: guild is None")  # ADD
                return

            channel = self.bot.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(channel_id)
                    log.warning("DEBUG fetched channel=%s", channel)  # ADD
                except BaseException:
                    log.exception("DEBUG _finish_duel CRASHED or CANCELLED")
                    raise
                finally:
                    self._duel_finishing.discard(key)
            log.warning("DEBUG channel type=%s", type(channel))  # ADD
            
            log.warning("DEBUG _finish_duel: scoring, current_q=%s total=%s", state.current_question, state.total_questions)  # ADD

            scored: List[Tuple[int, int, int, int]] = []
            for uid, (guess_year, guessed_at) in state.guesses.items():
                diff = abs(guess_year - state.correct_year)
                scored.append((diff, guessed_at, uid, guess_year))
            scored.sort(key=lambda x: (x[0], x[1]))

            winner_user_id: Optional[int] = None
            winner_guess: Optional[int] = None
            winner_diff: Optional[int] = None
            if scored:
                winner_diff, _ts, winner_user_id, winner_guess = scored[0]
                state.scores[winner_user_id] = int(state.scores.get(winner_user_id, 0)) + 1
                log.warning("DEBUG after scoring: winner=%s", winner_user_id)  # ADD

            result = DuelQuestionResult(
                question_number=state.current_question,
                event_id=state.event_id,
                prompt=state.prompt,
                correct_year=state.correct_year,
                guesses={uid: guess_year for uid, (guess_year, _ts) in state.guesses.items()},
                winner_user_id=winner_user_id,
                winner_guess=winner_guess,
                winner_diff=winner_diff,
            )
            state.history.append(result)
            log.warning("DEBUG after result built")  # ADD

            if isinstance(channel, (discord.TextChannel, discord.Thread)):
                question_embed = self._build_duel_result_embed(guild, state, result, forced=forced)
                log.warning("DEBUG after embed built")  # ADD
                try:
                    await asyncio.wait_for(channel.send(embed=question_embed), timeout=10.0)
                except asyncio.TimeoutError:
                        log.warning("DEBUG _finish_duel: channel.send timed out")

            if not forced and state.current_question < state.total_questions:
                log.warning("DEBUG _finish_duel: picking next event")  # ADD
                used_ids = {str(r.event_id) for r in state.history}
                next_evt = self._pick_duel_event(guild_id, channel_id, used_event_ids=used_ids)
                log.warning("DEBUG _finish_duel: next_evt=%s", next_evt)  # ADD
                if next_evt is None:  # ADD
                    if isinstance(channel, (discord.TextChannel, discord.Thread)):  # ADD
                        await channel.send("⚠️ Debug: no next event found, ending duel early.")  # ADD
                if next_evt is not None:
                    state.current_question += 1
                    state.event_id = str(next_evt["id"])
                    state.correct_year = int(next_evt["year"])
                    state.prompt = str(next_evt["prompt"])
                    state.started_at = int(time.time())
                    state.ends_at = state.started_at + int(self.bot.cfg.GUESSYEAR_ROUND_SECONDS)
                    state.guesses.clear()
                    self._schedule_duel_end(state)
                    if isinstance(channel, (discord.TextChannel, discord.Thread)):
                        await channel.send(
                            f"⚔️ Next duel question: **{state.current_question}/{state.total_questions}**. A new hidden-guess panel is below."
                        )
                        await self._replace_duel_round_message(guild, channel, state)
                    else:
                        await self._refresh_duel_message(guild_id, channel_id)
                    return

            view = self._duel_round_views.pop(key, None)
            if view and view.message is not None:
                try:
                    await view.message.edit(view=None)
                except Exception:
                    pass

            self._duel_active.pop(key, None)

            last_evt = self._events_by_id.get(result.event_id)
            if result.winner_user_id is not None and result.winner_diff == 0 and last_evt and self._bonus_modes_for_event(last_evt):
                self._recent_finished[key] = {
                    "round_id": 0,
                    "event_id": result.event_id,
                    "winner_user_id": int(result.winner_user_id),
                    "unlocked_at": int(time.time()),
                }
            else:
                self._recent_finished.pop(key, None)
                self._bonus_active.pop(key, None)

            if isinstance(channel, (discord.TextChannel, discord.Thread)):
                match_embed = self._build_duel_match_result_embed(guild, state, forced=forced)
                await channel.send(embed=match_embed)

                if result.winner_user_id is not None and result.winner_diff == 0 and last_evt and self._bonus_modes_for_event(last_evt):
                    modes = self._bonus_modes_for_event(last_evt)
                    if len(modes) == 1:
                        modes_text = f"`!bonus {modes[0]}`"
                    else:
                        modes_text = "`!bonus month` or `!bonus person`"
                    await channel.send(
                        f"🎁 <@{result.winner_user_id}> unlocked a bonus round for the final duel question. Start it with {modes_text}."
                    )
                    
            if isinstance(channel, discord.Thread) and state.duel_thread_created:
                await self._prompt_thread_close(channel, state)

        except Exception:
            log.exception("DEBUG _finish_duel CRASHED")
            raise
        finally:
            self._duel_finishing.discard(key)


    # ---------- round end / announce ----------

    async def _end_round(self, guild_id: int, channel_id: int, forced: bool) -> None:
        key = (guild_id, channel_id)
        state = self._active.get(key)
        if not state:
            return

        try:
            did_end = await self.bot.db.guessyear_try_end_round(state.round_id)
        except Exception:
            log.exception("Failed to end GuessYear round in DB")
            return

        if not did_end:
            # Another task/process already ended and announced.
            self._active.pop(key, None)
            return

        # Pull guesses
        try:
            guesses = await self.bot.db.guessyear_list_guesses(state.round_id)
        except Exception:
            log.exception("Failed to fetch GuessYear guesses")
            guesses = []

        # Score guesses (diff asc, earliest ts wins ties)
        scored: List[Tuple[int, int, int, int]] = []  # (abs_diff, guessed_at, user_id, guess_year)
        for g in guesses:
            try:
                uid = int(g["user_id"])
                gy = int(g["guess_year"])
                ts = int(g["guessed_at"])
                diff = abs(gy - state.correct_year)
                scored.append((diff, ts, uid, gy))
            except Exception:
                continue

        scored.sort(key=lambda x: (x[0], x[1]))

        winner_user_id: Optional[int] = None
        winner_guess: Optional[int] = None
        winner_diff: Optional[int] = None
        if scored:
            winner_diff, _ts, winner_user_id, winner_guess = scored[0]

        # Stats: count plays for all guessers; count win for winner.
        try:
            await self.bot.db.guessyear_stats_record_play(guild_id, [int(g["user_id"]) for g in guesses])
            if winner_user_id is not None:
                await self.bot.db.guessyear_stats_record_win(guild_id, int(winner_user_id))
        except Exception:
            # Stats are optional; never fail the round end.
            pass

        evt = self._events_by_id.get(state.event_id)
        if winner_user_id is not None and winner_diff == 0 and evt and self._bonus_modes_for_event(evt):
            self._recent_finished[key] = {
                "round_id": state.round_id,
                "event_id": state.event_id,
                "winner_user_id": int(winner_user_id),
                "unlocked_at": int(time.time()),
            }
        else:
            self._recent_finished.pop(key, None)
            self._bonus_active.pop(key, None)

        # Fetch channel
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except Exception:
                channel = None

        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            unique_players = {int(g["user_id"]) for g in guesses}
            total_guesses = len(guesses)
            total_players = len(unique_players)

            lines: List[str] = []
            lines.append(f"**Prompt:** {state.prompt}")
            lines.append(f"**Correct year:** **{state.correct_year}**")
            lines.append(f"**Guesses:** **{total_guesses}** ({total_players} player{'s' if total_players != 1 else ''})")

            if forced:
                lines.append("_Round was ended by a moderator._")

            if winner_user_id is None:
                lines.append("\nNo valid guesses were submitted. No winner this round.")
            else:
                perfect = " 🎯" if (winner_diff == 0) else ""
                lines.append(
                    f"\n🏆 **Winner:** <@{winner_user_id}> (guessed **{winner_guess}**, off by **{winner_diff}**){perfect}"
                )

                top = scored[:3]
                lines.append("\n**Top 3 closest:**")
                for i, (diff, _ts, uid, gy) in enumerate(top, start=1):
                    lines.append(f"{i}. <@{uid}> — **{gy}** (off by **{diff}**)")

            msg = f"**🕰️ Guess the Year — Round #{state.round_id} ended**\n\n" + "\n".join(lines)
            await channel.send(msg)

            if winner_user_id is not None and winner_diff == 0 and evt and self._bonus_modes_for_event(evt):
                modes = self._bonus_modes_for_event(evt)
                if len(modes) == 1:
                    modes_text = f"`!bonus {modes[0]}`"
                else:
                    modes_text = "`!bonus month` or `!bonus person`"
                await channel.send(
                    f"🎁 <@{winner_user_id}> unlocked a bonus round for this event. "
                    f"Start it with {modes_text}."
                )

        # Cleanup memory + timer
        self._active.pop(key, None)
        task = self._end_tasks.pop(key, None)
        if task and not task.done():
            task.cancel()

    # ---------- commands ----------

    @commands.group(name="guessyear", invoke_without_command=True)
    async def guessyear(self, ctx: commands.Context):
        """Start a Guess the Year round in this channel."""
        if not ctx.guild or not isinstance(ctx.channel, (discord.TextChannel, discord.Thread)):
            return

        if not self.bot.cfg.GUESSYEAR_ENABLED:
            return await ctx.send("Guess the Year is disabled on this server.", delete_after=10)

        if not self._is_allowed_channel(ctx.channel.id):
            return await ctx.send("Guess the Year is not enabled in this channel.", delete_after=10)

        busy = self._duel_busy_message(ctx.guild.id, ctx.channel.id)
        if busy:
            return await ctx.send(f"{busy} Finish or cancel it before starting a normal round.", delete_after=12)

        self._cleanup_expired_bonus(ctx.guild.id, ctx.channel.id)
        active_bonus = self._bonus_active.get((ctx.guild.id, ctx.channel.id))
        if active_bonus and active_bonus.ends_at > int(time.time()):
            rem = self._remaining(active_bonus.ends_at)
            return await ctx.send(
                f"A bonus round is active here. **{rem}s** remaining. "
                f"Finish it with `!bonus <answer>` before starting a new round.",
                delete_after=12,
            )

        state = await self._ensure_state_loaded(ctx.guild.id, ctx.channel.id)
        if state and state.ends_at > int(time.time()):
            rem = self._remaining(state.ends_at)
            return await ctx.send(
                f"A round is already active here. **{rem}s** remaining. "
                f"Type a year (e.g. `1066`) to guess. Use `!hint`.",
                delete_after=12,
            )

        evt = self._pick_event(ctx.guild.id, ctx.channel.id)
        if not evt:
            return await ctx.send(
                "No GuessYear events match this channel's selected categories. Use `!categories reset` or choose more categories.",
                delete_after=12,
            )

        min_year = int(self.bot.cfg.GUESSYEAR_MIN_YEAR)
        max_year = self._resolve_max_year()
        correct_year = int(evt["year"])
        if correct_year > max_year:
            return await ctx.send("Dataset event has an out-of-range year. Please fix the dataset.", delete_after=12)

        now = int(time.time())
        ends_at = now + int(self.bot.cfg.GUESSYEAR_ROUND_SECONDS)

        try:
            round_id = await self.bot.db.guessyear_create_round(
                guild_id=ctx.guild.id,
                channel_id=ctx.channel.id,
                started_by_user_id=ctx.author.id,
                event_id=str(evt["id"]),
                correct_year=correct_year,
                started_at=now,
                ends_at=ends_at,
            )
        except sqlite3.IntegrityError:
            # Another task already created an active round.
            state = await self._ensure_state_loaded(ctx.guild.id, ctx.channel.id)
            if state and state.ends_at > int(time.time()):
                rem = self._remaining(state.ends_at)
                return await ctx.send(
                    f"A round is already active here. **{rem}s** remaining. "
                    f"Type a year (e.g. `1066`) to guess. Use `!hint`.",
                    delete_after=12,
                )
            return await ctx.send("A round is already active here.", delete_after=10)
        except Exception:
            log.exception("Failed to create GuessYear round in DB")
            return await ctx.send("Could not start a round (database error).", delete_after=12)

        state = RoundState(
            round_id=int(round_id),
            guild_id=ctx.guild.id,
            channel_id=ctx.channel.id,
            event_id=str(evt["id"]),
            correct_year=correct_year,
            prompt=str(evt["prompt"]),
            hints=list(evt.get("hints", [])),
            started_at=now,
            ends_at=ends_at,
            hints_used=0,
        )
        self._active[(ctx.guild.id, ctx.channel.id)] = state
        self._schedule_end(state)

        category_text = self._format_category_list(self._categories_for_channel(ctx.guild.id, ctx.channel.id))
        await ctx.send(
            f"**🕰️ Guess the Year #{round_id}**\n"
            f"**Prompt:** {state.prompt}\n"
            f"**Categories:** {category_text}\n\n"
            f"Submit your guess as a year (e.g., `1066`).\n"
            f"Time: **{self.bot.cfg.GUESSYEAR_ROUND_SECONDS}s**. Use `!hint` for clues."
        )

    @guessyear.command(name="status")
    async def guessyear_status(self, ctx: commands.Context):
        if not ctx.guild:
            return
        state = await self._ensure_state_loaded(ctx.guild.id, ctx.channel.id)
        if not state or state.ends_at <= int(time.time()):
            return await ctx.send("No active Guess the Year round in this channel.", delete_after=10)

        rem = self._remaining(state.ends_at)
        cats = self._format_category_list(self._categories_for_channel(ctx.guild.id, ctx.channel.id))
        await ctx.send(
            f"🕰️ Round #{state.round_id} is active. **{rem}s** remaining.\n"
            f"Hints used: **{state.hints_used}/{self.bot.cfg.GUESSYEAR_MAX_HINTS}**.\n"
            f"Categories: {cats}.\n"
            f"Guess by typing a year like `1789`."
        )

    @guessyear.command(name="stop")
    async def guessyear_stop(self, ctx: commands.Context):
        if not ctx.guild:
            return

        if not isinstance(ctx.author, discord.Member) or not self._can_manage_rounds(ctx.author):
            return await ctx.send("You don't have permission to stop the round.", delete_after=10)

        state = await self._ensure_state_loaded(ctx.guild.id, ctx.channel.id)
        if not state:
            return await ctx.send("No active round to stop in this channel.", delete_after=10)

        await ctx.send("Ending the current round…", delete_after=5)
        await self._end_round(ctx.guild.id, ctx.channel.id, forced=True)

    @guessyear.command(name="play")
    async def guessyear_play(self, ctx: commands.Context, event_id: str):
        """Start a Guess the Year round with a specific event ID. Mod only."""
        if not ctx.guild or not isinstance(ctx.channel, (discord.TextChannel, discord.Thread)):
            return

        if not self.bot.cfg.GUESSYEAR_ENABLED:
            return await ctx.send("Guess the Year is disabled on this server.", delete_after=10)

        if not self._is_allowed_channel(ctx.channel.id):
            return await ctx.send("Guess the Year is not enabled in this channel.", delete_after=10)

        if not isinstance(ctx.author, discord.Member) or not self._can_manage_rounds(ctx.author):
            return await ctx.send("You don't have permission to use this command.", delete_after=10)

        busy = self._duel_busy_message(ctx.guild.id, ctx.channel.id)
        if busy:
            return await ctx.send(f"{busy} Finish or cancel it before starting a normal round.", delete_after=12)

        state = await self._ensure_state_loaded(ctx.guild.id, ctx.channel.id)
        if state and state.ends_at > int(time.time()):
            rem = self._remaining(state.ends_at)
            return await ctx.send(f"A round is already active here. **{rem}s** remaining.", delete_after=12)

        evt = self._events_by_id.get(str(event_id))
        if not evt:
            return await ctx.send(f"No event found with ID `{event_id}`.", delete_after=12)

        min_year = int(self.bot.cfg.GUESSYEAR_MIN_YEAR)
        max_year = self._resolve_max_year()
        correct_year = int(evt["year"])
        if correct_year > max_year:
            return await ctx.send("That event has an out-of-range year.", delete_after=12)

        now = int(time.time())
        ends_at = now + int(self.bot.cfg.GUESSYEAR_ROUND_SECONDS)

        try:
            round_id = await self.bot.db.guessyear_create_round(
                guild_id=ctx.guild.id,
                channel_id=ctx.channel.id,
                started_by_user_id=ctx.author.id,
                event_id=str(evt["id"]),
                correct_year=correct_year,
                started_at=now,
                ends_at=ends_at,
            )
        except Exception:
            log.exception("Failed to create GuessYear round in DB")
            return await ctx.send("Could not start a round (database error).", delete_after=12)

        state = RoundState(
            round_id=int(round_id),
            guild_id=ctx.guild.id,
            channel_id=ctx.channel.id,
            event_id=str(evt["id"]),
            correct_year=correct_year,
            prompt=str(evt["prompt"]),
            hints=list(evt.get("hints", [])),
            started_at=now,
            ends_at=ends_at,
            hints_used=0,
        )
        self._active[(ctx.guild.id, ctx.channel.id)] = state
        self._schedule_end(state)

        await ctx.send(
            f"**🕰️ Guess the Year #{round_id}**\n"
            f"**Prompt:** {state.prompt}\n\n"
            f"Submit your guess as a year (e.g., `1066`).\n"
            f"Time: **{self.bot.cfg.GUESSYEAR_ROUND_SECONDS}s**. Use `!hint` for clues."
        )   

    @commands.command(name="duel")
    async def duel(self, ctx: commands.Context, opponent: discord.Member, questions: int = 1):
        if not ctx.guild or not isinstance(ctx.channel, (discord.TextChannel, discord.Thread)):
            return

        if not self.bot.cfg.GUESSYEAR_ENABLED:
            return await ctx.send("Guess the Year is disabled on this server.", delete_after=10)

        if not self._is_allowed_channel(ctx.channel.id):
            return await ctx.send("Guess the Year is not enabled in this channel.", delete_after=10)

        if opponent.bot:
            return await ctx.send("You cannot duel a bot.", delete_after=10)
        if opponent.id == ctx.author.id:
            return await ctx.send("You cannot duel yourself.", delete_after=10)

        if questions < 1 or questions > 10:
            return await ctx.send("Choose a duel length between **1** and **10** questions. Example: `!duel @user 3`", delete_after=10)

        self._cleanup_expired_bonus(ctx.guild.id, ctx.channel.id)
        active_bonus = self._bonus_active.get((ctx.guild.id, ctx.channel.id))
        if active_bonus and active_bonus.ends_at > int(time.time()):
            return await ctx.send("A bonus round is active here. Finish it before starting a duel.", delete_after=10)

        state = await self._ensure_state_loaded(ctx.guild.id, ctx.channel.id)
        if state and state.ends_at > int(time.time()):
            return await ctx.send("A normal GuessYear round is active here. Finish it before starting a duel.", delete_after=10)

        busy = self._duel_busy_message(ctx.guild.id, ctx.channel.id)
        if busy:
            return await ctx.send(busy, delete_after=10)

        if not self._events_for_channel(ctx.guild.id, ctx.channel.id):
            return await ctx.send(
                "No GuessYear events match this channel's selected categories. Use `!categories reset` or choose more categories.",
                delete_after=12,
            )

        now = int(time.time())
        challenge = DuelChallengeState(
            guild_id=ctx.guild.id,
            channel_id=ctx.channel.id,
            challenger_user_id=ctx.author.id,
            opponent_user_id=opponent.id,
            total_questions=int(questions),
            created_at=now,
            expires_at=now + 60,
        )
        key = (ctx.guild.id, ctx.channel.id)
        self._duel_challenges[key] = challenge

        view = DuelChallengeView(self, challenge)
        self._duel_challenge_views[key] = view
        embed = self._build_duel_challenge_embed(ctx.guild, challenge)
        message = await ctx.send(content=opponent.mention, embed=embed, view=view)
        view.message = message

    @commands.command(name="duelstatus")
    async def duelstatus(self, ctx: commands.Context):
        if not ctx.guild:
            return

        key = (ctx.guild.id, ctx.channel.id)

        challenge = self._find_duel_challenge_for_context(ctx.guild.id, ctx.channel.id)
        if challenge and challenge.expires_at > int(time.time()):
            embed = self._build_duel_challenge_embed(ctx.guild, challenge)
            return await ctx.send(embed=embed)

        duel = self._find_duel_for_context(ctx.guild.id, ctx.channel.id)
        if duel and duel.ends_at > int(time.time()):
            embed = self._build_duel_round_embed(ctx.guild, duel)
            if duel.channel_id != ctx.channel.id:
                duel_channel = self.bot.get_channel(duel.channel_id)
                if duel_channel is None:
                    try:
                        duel_channel = await self.bot.fetch_channel(duel.channel_id)
                    except Exception:
                        duel_channel = None
                if isinstance(duel_channel, discord.Thread):
                    embed.add_field(name="Duel thread", value=duel_channel.mention, inline=False)
            return await ctx.send(embed=embed)

        return await ctx.send("No duel challenge or active duel in this channel.", delete_after=10)

    @commands.command(name="duelcancel")
    async def duelcancel(self, ctx: commands.Context):
        if not ctx.guild or not isinstance(ctx.channel, (discord.TextChannel, discord.Thread)):
            return

        key = (ctx.guild.id, ctx.channel.id)
        member = ctx.author if isinstance(ctx.author, discord.Member) else None

        challenge = self._find_duel_challenge_for_context(ctx.guild.id, ctx.channel.id)
        if challenge:
            allowed = (
                member is not None and (
                    member.id in (challenge.challenger_user_id, challenge.opponent_user_id)
                    or self._can_manage_rounds(member)
                )
            )
            if not allowed:
                return await ctx.send("Only the duel participants or a moderator can cancel this duel challenge.", delete_after=10)

            self._duel_challenges.pop(key, None)
            view = self._duel_challenge_views.pop(key, None)
            if view and view.message is not None:
                try:
                    embed = discord.Embed(
                        title="⚔️ GuessYear Duel Challenge",
                        description="The duel challenge was cancelled.",
                        color=discord.Color.dark_red(),
                    )
                    await view.message.edit(embed=embed, view=None)
                except Exception:
                    pass
            return await ctx.send("Duel challenge cancelled.", delete_after=8)

        duel = self._find_duel_for_context(ctx.guild.id, ctx.channel.id)
        if duel:
            allowed = (
                member is not None and (
                    member.id in (duel.challenger_user_id, duel.opponent_user_id)
                    or self._can_manage_rounds(member)
                )
            )
            if not allowed:
                return await ctx.send("Only the duel participants or a moderator can cancel this duel.", delete_after=10)

            await self._finish_duel(ctx.guild.id, ctx.channel.id, forced=True)
            return

        await ctx.send("No duel challenge or active duel to cancel in this channel.", delete_after=10)

    ### Guess Year Leaderboard
    @guessyear.command(name="top")
    async def guessyear_top(self, ctx: commands.Context, limit: int = 10):
        """Show the GuessYear leaderboard for this server."""
        if not ctx.guild:
            return

        limit = max(1, min(int(limit), 25))

        try:
            rows = await self.bot.db.guessyear_stats_get_top(ctx.guild.id, limit)
        except Exception:
            log.exception("Failed reading GuessYear leaderboard")
            return await ctx.send("Could not fetch leaderboard (database error).", delete_after=10)

        if not rows:
            return await ctx.send("No GuessYear stats yet. Play a few rounds first!", delete_after=10)

        def rank_prefix(rank: int) -> str:
            if rank == 1:
                return "🥇"
            if rank == 2:
                return "🥈"
            if rank == 3:
                return "🥉"
            return f"#{rank}"

        lines = []
        for i, r in enumerate(rows, start=1):
            uid = int(r["user_id"])
            wins = int(r["wins"])
            plays = int(r["plays"])
            member = ctx.guild.get_member(uid)
            mention = member.mention if member else f"<@{uid}>"

            if not member:
                try:
                    member = await ctx.guild.fetch_member(uid)
                except Exception:
                    member = None

            raw_name = member.display_name if member else f"User {uid}"
            discord.utils.escape_markdown(raw_name)
            rate = (wins / plays * 100.0) if plays > 0 else 0.0

            lines.append(
                f"{rank_prefix(i)} **{mention}**\n"
                f"🏆 **{wins}** wins • 🎲 **{plays}** plays • 📈 **{rate:.0f}%** win rate"
            )

        embed = discord.Embed(
            title="🏅 Guess Year Leaderboard",
            description="\n\n".join(lines),
            color=discord.Color.gold(),
        )

        embed.set_footer(
            text=f"Server: {ctx.guild.name} • Top {min(len(rows), limit)} players"
        )

        await ctx.send(embed=embed)

    @guessyear.command(name="me")
    async def guessyear_me(self, ctx: commands.Context):
        """Show your GuessYear stats in this server."""
        if not ctx.guild:
            return

        try:
            row = await self.bot.db.guessyear_stats_get_user(ctx.guild.id, ctx.author.id)
        except Exception:
            log.exception("Failed reading GuessYear user stats")
            return await ctx.send("Could not fetch your stats (database error).", delete_after=10)

        if not row:
            return await ctx.send("You don't have any GuessYear stats yet — play a round to get started!", delete_after=12)

        wins = int(row["wins"])
        plays = int(row["plays"])
        rate = (wins / plays * 100.0) if plays > 0 else 0.0
        rank = int(row["rank"]) if row.get("rank") is not None else None
        total = int(row["total"]) if row.get("total") is not None else None
        last_played = int(row.get("last_played_at") or 0)

        when = f"<t:{last_played}:R>" if last_played else "never"
        rank_str = f"#{rank} of {total}" if rank and total else "(unranked)"

        await ctx.send(
            "\n".join(
                [
                    f"**📊 GuessYear stats for {ctx.author.mention}**",
                    f"Rank: **{rank_str}**",
                    f"Wins: **{wins}**",
                    f"Plays: **{plays}**",
                    f"Win rate: **{rate:.0f}%**",
                    f"Last played: **{when}**",
                ]
            )
        )

    @commands.group(name="categories", invoke_without_command=True)
    async def categories(self, ctx: commands.Context):
        if not ctx.guild or not isinstance(ctx.channel, (discord.TextChannel, discord.Thread)):
            return
        if not self.bot.cfg.GUESSYEAR_ENABLED:
            return await ctx.send("Guess the Year is disabled on this server.", delete_after=10)
        if not self._is_allowed_channel(ctx.channel.id):
            return await ctx.send("Guess the Year is not enabled in this channel.", delete_after=10)

        view = GuessYearCategoriesView(self, ctx.author.id, ctx.guild.id, ctx.channel.id)
        embed = self._build_categories_embed(ctx.guild.id, ctx.channel.id, ctx.author)
        message = await ctx.send(embed=embed, view=view)
        view.message = message

    @categories.command(name="show")
    async def categories_show(self, ctx: commands.Context):
        if not ctx.guild or not isinstance(ctx.channel, (discord.TextChannel, discord.Thread)):
            return
        embed = self._build_categories_embed(ctx.guild.id, ctx.channel.id, ctx.author)
        await ctx.send(embed=embed)

    @categories.command(name="reset")
    async def categories_reset(self, ctx: commands.Context):
        if not ctx.guild or not isinstance(ctx.channel, (discord.TextChannel, discord.Thread)):
            return

        self._channel_categories.pop((ctx.guild.id, ctx.channel.id), None)
        embed = self._build_categories_embed(ctx.guild.id, ctx.channel.id, ctx.author)
        embed.description = "This channel has been reset to **all categories** for future GuessYear rounds."
        await ctx.send(embed=embed)

    @commands.command(name="learn")
    async def learn(self, ctx: commands.Context):
        if not ctx.guild or not isinstance(ctx.channel, (discord.TextChannel, discord.Thread)):
            return
        if not self.bot.cfg.GUESSYEAR_ENABLED:
            return await ctx.send("Guess the Year is disabled on this server.", delete_after=10)
        if not self._is_allowed_channel(ctx.channel.id):
            return await ctx.send("Guess the Year is not enabled in this channel.", delete_after=10)
        if self._find_duel_for_context(ctx.guild.id, ctx.channel.id):
            return await ctx.send("A duel is active here. Start learning from a normal text channel instead.", delete_after=10)

        owner_key = (ctx.guild.id, ctx.author.id)
        existing_thread_id = self._learn_owner_threads.get(owner_key)
        if existing_thread_id:
            existing = self.bot.get_channel(existing_thread_id)
            if existing is None:
                try:
                    existing = await self.bot.fetch_channel(existing_thread_id)
                except Exception:
                    existing = None
            if isinstance(existing, discord.Thread):
                return await ctx.send(f"You already have a learning thread open: {existing.mention}", delete_after=12)
            self._learn_owner_threads.pop(owner_key, None)

        if isinstance(ctx.channel, discord.Thread):
            return await ctx.send("Start `!learn` from a normal server text channel so I can create your private practice thread.", delete_after=10)

        evt = self._pick_learn_event(self._categories_for_channel(ctx.guild.id, ctx.channel.id))
        if evt is None:
            return await ctx.send("No practice events are available for this category selection.", delete_after=10)

        try:
            thread = await self._create_learn_thread(ctx)
        except Exception as e:
            log.exception("Failed to create learn thread")
            return await ctx.send(
                f"I couldn't create your private practice thread: `{type(e).__name__}: {e}`",
                delete_after=15,
            )

        now = int(time.time())
        state = LearnSessionState(
            guild_id=ctx.guild.id,
            channel_id=thread.id,
            host_channel_id=ctx.channel.id,
            owner_user_id=ctx.author.id,
            current_event_id=str(evt["id"]),
            correct_year=int(evt["year"]),
            prompt=str(evt["prompt"]),
            category_keys=self._categories_for_channel(ctx.guild.id, ctx.channel.id),
            started_at=now,
            used_event_ids=[str(evt["id"])],
        )
        self._learn_active[(ctx.guild.id, thread.id)] = state
        self._learn_owner_threads[owner_key] = thread.id

        await ctx.send(f"📚 Your private practice thread is ready: {thread.mention}", delete_after=15)
        await thread.send(embed=self._build_learn_intro_embed(ctx.author, state))
        await self._post_learn_question(thread, state)

    @commands.command(name="learnhint")
    async def learnhint(self, ctx: commands.Context):
        if not ctx.guild:
            return
        state = self._learn_active.get((ctx.guild.id, ctx.channel.id))
        if not state:
            return await ctx.send("No active learning session in this thread.", delete_after=8)
        if ctx.author.id != state.owner_user_id:
            return await ctx.send("Only the learner who owns this session can request hints.", delete_after=8)
        evt = self._events_by_id.get(state.current_event_id) or {}
        hints = list(evt.get("hints", []))
        if not state.awaiting_answer:
            return await ctx.send("This question is already answered. Use `!learnnext` for another one.", delete_after=8)
        if state.current_hints_used >= len(hints):
            return await ctx.send("No more hints are available for this practice question.", delete_after=8)
        hint_text = str(hints[state.current_hints_used])
        state.current_hints_used += 1
        await ctx.send(f"💡 Hint {state.current_hints_used}/{len(hints)}: **{hint_text}**")

    @commands.command(name="learnnext")
    async def learnnext(self, ctx: commands.Context):
        if not ctx.guild:
            return
        state = self._learn_active.get((ctx.guild.id, ctx.channel.id))
        if not state:
            return await ctx.send("No active learning session in this thread.", delete_after=8)
        if ctx.author.id != state.owner_user_id:
            return await ctx.send("Only the learner who owns this session can request the next question.", delete_after=8)
        evt = self._pick_learn_event(state.category_keys, used_event_ids=set(state.used_event_ids))
        if evt is None:
            return await ctx.send("No more practice events are available for this category selection.", delete_after=8)
        state.current_event_id = str(evt["id"])
        state.correct_year = int(evt["year"])
        state.prompt = str(evt["prompt"])
        state.current_hints_used = 0
        state.awaiting_answer = True
        state.used_event_ids.append(state.current_event_id)
        await self._post_learn_question(ctx.channel, state)

    @commands.command(name="learnstop")
    async def learnstop(self, ctx: commands.Context):
        if not ctx.guild:
            return
        state = self._learn_active.get((ctx.guild.id, ctx.channel.id))
        if not state:
            return await ctx.send("No active learning session in this thread.", delete_after=8)
        member = ctx.author if isinstance(ctx.author, discord.Member) else None
        if ctx.author.id != state.owner_user_id and not (member and self._can_manage_rounds(member)):
            return await ctx.send("Only the learner or a moderator can end this session.", delete_after=8)
        key = (state.guild_id, state.channel_id)
        self._learn_active.pop(key, None)
        self._learn_owner_threads.pop((state.guild_id, state.owner_user_id), None)
        self._learn_views.pop(key, None)
        embed = discord.Embed(
            title="📚 Learning Session Ended",
            description=(
                f"Questions completed: **{state.questions_answered}**\n"
                f"Exact guesses: **{state.exact_hits}**"
            ),
            color=discord.Color.dark_grey(),
        )
        await ctx.send(embed=embed)

    @commands.command(name="hint")
    async def hint(self, ctx: commands.Context):
        if not ctx.guild:
            return
        if not self.bot.cfg.GUESSYEAR_HINTS_ENABLED:
            return await ctx.send("Hints are disabled.", delete_after=8)

        if (ctx.guild.id, ctx.channel.id) in self._duel_active:
            return await ctx.send("Hints are not available during hidden duel rounds.", delete_after=10)

        state = await self._ensure_state_loaded(ctx.guild.id, ctx.channel.id)
        if not state or state.ends_at <= int(time.time()):
            return await ctx.send("No active round in this channel.", delete_after=10)

        max_hints = int(self.bot.cfg.GUESSYEAR_MAX_HINTS)
        if state.hints_used >= max_hints:
            return await ctx.send("No more hints available this round.", delete_after=10)

        if not state.hints:
            return await ctx.send("This event has no hints configured.", delete_after=10)

        next_index = state.hints_used
        if next_index >= len(state.hints):
            return await ctx.send("No more hints available for this event.", delete_after=10)

        try:
            new_used = await self.bot.db.guessyear_increment_hints_used(state.round_id)
        except Exception:
            log.exception("Failed incrementing hints_used")
            return await ctx.send("Could not fetch a hint (database error).", delete_after=10)

        state.hints_used = int(new_used)
        hint_text = state.hints[next_index]
        await ctx.send(f"💡 Hint {next_index+1}/{max_hints}: **{hint_text}**")

    @commands.command(name="guess")
    async def guess_cmd(self, ctx: commands.Context, year: str):
        # Explicit command form: !guess 1789
        await self._handle_guess(ctx.message, override_text=year)

    @commands.command(name="bonus")
    async def bonus(self, ctx: commands.Context, *, arg: Optional[str] = None):
        if not ctx.guild or not isinstance(ctx.channel, (discord.TextChannel, discord.Thread)):
            return
        if not self.bot.cfg.GUESSYEAR_ENABLED:
            return

        key = (ctx.guild.id, ctx.channel.id)
        now = int(time.time())
        self._cleanup_expired_bonus(ctx.guild.id, ctx.channel.id)

        active = self._bonus_active.get(key)
        requested = (arg or "").strip()

        # Active bonus: answer it or re-show it
        if active and active.ends_at > now:
            if ctx.author.id != active.winner_user_id:
                return await ctx.send("Only the exact-year winner can answer this bonus round.", delete_after=8)

            if not requested:
                member = ctx.guild.get_member(active.winner_user_id)
                return await ctx.send(embed=self._build_bonus_embed(active, ctx.guild, member))

            if self._bonus_matches(requested, active.answers):
                self._bonus_active.pop(key, None)
                embed = discord.Embed(
                    title="🎉 Bonus Correct!",
                    description=f"{ctx.author.mention} got the bonus question right.",
                    color=discord.Color.green(),
                )
                embed.add_field(name="Accepted answer", value=f"**{requested}**", inline=False)
                if active.source_round_id:
                    embed.set_footer(text=f"From GuessYear round #{active.source_round_id}")
                else:
                    embed.set_footer(text="From a GuessYear duel")
                return await ctx.send(embed=embed)

            return await ctx.send("❌ Not quite. Try `!bonus <answer>` again before time runs out.", delete_after=8)

        recent = self._recent_finished.get(key)
        if not recent:
            return await ctx.send("No recent exact-year win with a bonus is available in this channel.", delete_after=10)

        if ctx.author.id != int(recent["winner_user_id"]):
            return await ctx.send("Only the winner of the previous exact-year round can start the bonus.", delete_after=10)

        evt = self._events_by_id.get(str(recent["event_id"]))
        if not evt:
            return await ctx.send("The previous event could not be loaded.", delete_after=10)

        modes = self._bonus_modes_for_event(evt)
        if not modes:
            self._recent_finished.pop(key, None)
            return await ctx.send("This event does not have bonus data configured.", delete_after=10)

        requested_mode = requested.lower() if requested else None
        if requested_mode and requested_mode not in ("month", "person"):
            return await ctx.send(
                "Use `!bonus month` or `!bonus person` to start the bonus, then `!bonus <answer>` to answer it.",
                delete_after=10,
            )

        picked = self._pick_bonus_definition(evt, requested_mode=requested_mode)
        if picked is None:
            if requested_mode:
                return await ctx.send(f"This event does not have a `{requested_mode}` bonus.", delete_after=10)

            if len(modes) > 1:
                return await ctx.send(
                    "Choose a bonus type with `!bonus month` or `!bonus person`.",
                    delete_after=12,
                )

            picked = self._pick_bonus_definition(evt, requested_mode=modes[0])

        if picked is None:
            return await ctx.send("Could not start the bonus round for this event.", delete_after=10)

        mode, info = picked
        bonus_state = BonusState(
            guild_id=ctx.guild.id,
            channel_id=ctx.channel.id,
            event_id=str(recent["event_id"]),
            source_round_id=int(recent["round_id"]),
            winner_user_id=int(recent["winner_user_id"]),
            mode=mode,
            prompt=str(info["prompt"]),
            answers=[str(x) for x in info.get("answers", [])],
            started_at=now,
            ends_at=now + 60,
        )
        self._bonus_active[key] = bonus_state

        member = ctx.guild.get_member(bonus_state.winner_user_id)
        await ctx.send(embed=self._build_bonus_embed(bonus_state, ctx.guild, member))

    # ---------- message listener (guesses) ----------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Treat plain year messages as guesses.
        if message.author.bot:
            return
        if not message.guild:
            return

        if await self._handle_learn_guess(message):
            return

        if message.content and message.content.lstrip().startswith("!"):
            return

        await self._handle_guess(message, override_text=None)

    async def _handle_learn_guess(self, message: discord.Message) -> bool:
        state = self._learn_active.get((message.guild.id, message.channel.id))
        if not state:
            return False
        if message.author.id != state.owner_user_id:
            return True
        if message.content and message.content.lstrip().startswith("!"):
            return False
        if not state.awaiting_answer:
            return True

        m = YEAR_RE.match(message.content or "")
        if not m:
            return True

        guess_year = int(m.group(1))
        diff = abs(guess_year - state.correct_year)
        state.questions_answered += 1
        if diff == 0:
            state.exact_hits += 1
        state.awaiting_answer = False

        evt = self._events_by_id.get(state.current_event_id) or {
            "tags": [],
            "hints": [],
        }
        embed = self._build_learn_result_embed(state, guess_year, evt, diff)
        await message.channel.send(embed=embed)
        return True

    async def _handle_guess(self, message: discord.Message, override_text: Optional[str]):
        if not message.guild:
            return
        if not isinstance(message.channel, (discord.TextChannel, discord.Thread)):
            return
        if not self.bot.cfg.GUESSYEAR_ENABLED:
            return
        if not self._is_allowed_channel(message.channel.id):
            return

        if (message.guild.id, message.channel.id) in self._duel_active:
            return

        state = await self._ensure_state_loaded(message.guild.id, message.channel.id)
        if not state or state.ends_at <= int(time.time()):
            return

        now = int(time.time())

        # cooldown anti-spam
        key_cd = (message.guild.id, message.channel.id, message.author.id)
        last = self._guess_cooldown.get(key_cd, 0)
        if now - last < int(self.bot.cfg.GUESSYEAR_COOLDOWN_SECONDS):
            return
        self._guess_cooldown[key_cd] = now

        text = override_text if override_text is not None else message.content
        m = YEAR_RE.match(text or "")
        if not m:
            return

        guess_year = int(m.group(1))
        min_year = int(self.bot.cfg.GUESSYEAR_MIN_YEAR)
        max_year = self._resolve_max_year()
        if guess_year > max_year:
            return

        policy = str(getattr(self.bot.cfg, "GUESSYEAR_GUESS_POLICY", "first")).lower().strip()
        if policy not in ("first", "latest"):
            policy = "first"

        try:
            ok, already = await self.bot.db.guessyear_upsert_guess(
                round_id=state.round_id,
                user_id=message.author.id,
                guess_year=guess_year,
                guessed_at=now,
                policy=policy,
            )
        except Exception:
            log.exception("Failed saving guess")
            return

        if not ok:
            return

        if already and policy == "first":
            return

        try:
            await message.channel.send(f"✅ {message.author.mention} guessed **{guess_year}**.")
        except Exception:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(GuessYearCog(bot))