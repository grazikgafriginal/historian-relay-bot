# Historians of the House Team Bot (discord.py 2.x)

A Discord bot for history servers:
- Users submit questions via `/askhist` in approved channels
- Bot posts a visible origin message + optional thread
- Bot forwards a workflow embed to `#verified-historians` or (optional) a mod queue
- Verified historians/mods can Claim / Unclaim / Needs Context / Close / Publish Answer (reply-based or modal)
- Approved answers are reposted to the original thread (preferred) or origin channel
- SQLite persistence + restart recovery (views reattached)
- Proactively surfaces `/askhist` for unanswered questions asked as plain chat (see below)

## Setup

### 1) Create a bot + invite
Enable:
- `applications.commands`
- `bot`
- (Recommended) `Read Message History`, `Send Messages`, `Embed Links`, `Manage Threads`

### 2) Install
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3) Configure environment
Create `.env`:

### 4) Run
```bash
python -m historian_relay_bot.bot
```

## Usage
- `/askhist question:<text> tag:<optional> era:<optional>`
- `/askhist_status id:<number>`
- `/askhist_cancel id:<number>`
- Moderation:
  - `/askhist_blacklist user:<user> reason:<optional>`
  - `/askhist_unblacklist user:<user>`
  - `/askhist_config`
  - `/Put events in historian_relay_bot/data/guessyear_events.json`

Commands:

!guessyear

!guessyear status

!guessyear stop (mods)

!hint

guess by typing 1066 etc

## Notes
- Reply-based publish: post your answer as a reply to the forwarded embed in the historians channel, then press “Publish Answer”.
- Restart recovery: bot re-edits tracked queue/hist messages to reattach views.

## Proactive Ask Historians (passive question watch)
Watches configured channels (e.g. `#history-general`) for question-shaped chat
messages. If a question goes unanswered for a while, the bot posts an
in-channel reminder (reply) and DMs the asker, each with an **Ask Historians**
button. Pressing it (or a moderator using the **Ask Historians** message
context-menu action) posts a Historian Request pinging
`VERIFIED_HISTORIAN_ROLE_ID` in `HISTORIANS_CHANNEL_ID`. When a Historian
replies (to the question or to the request), the request is marked resolved
and the response is logged for future KPI reporting — nothing is ever shown
publicly.

A message must pass three lightweight (non-AI) checks to start a watch:
1. **Grammatically a question** — a "?" outside of any URL, in a clause that
   also contains an actual interrogative word (why/what/how/is/does/can/...).
   Filters out offers and banter that merely end in "?"
   (e.g. "Wanna see my list of generals?").
2. **About history** — contains a year, a century (e.g. "5th century"), or one
   of `ASKHIST_WATCH_TOPIC_KEYWORDS` (regions, empires, eras, general
   historical vocabulary). Filters out off-topic questions
   (e.g. "Can I have an icecream?").
3. **Not itself a reply** — a message that replies to something is treated as
   continuing an existing exchange, not dropping a fresh standalone question
   (e.g. "So you are more into prehistory?" replying to a prior message).

Known limitation: a question about a specific person/event with no year,
century, region, or era keyword (e.g. just "Was Napoleon a tyrant?") won't be
picked up automatically — moderators can still escalate it manually via the
**Ask Historians** message context-menu action.

Config (env or `CONFIG_JSON`):
- `ASKHIST_WATCH_ENABLED` (default `true`) — feature on/off switch.
- `ASKHIST_WATCH_CHANNEL_IDS` (default empty = disabled until set) — CSV of channel IDs to watch, e.g. the `#history-general` channel ID.
- `ASKHIST_WATCH_MIN_WORDS` (default `4`) — a message needs a `?` and at least this many words to be tracked.
- `ASKHIST_WATCH_TOPIC_KEYWORDS` (JSON config only, has a broad built-in default) — topic words that count as a historical signal.
- `ASKHIST_WATCH_DELAY_SECONDS` (default `7200`, ~2 hours) — how long to wait before reminding.
- `ASKHIST_WATCH_PING_COOLDOWN_MINUTES` (default `60`) — per-user cooldown between "Ask Historians" triggers.
- `ASKHIST_WATCH_PING_MAX_PER_DAY` (default `3`) — per-user daily cap on "Ask Historians" triggers.

Moderators (`MOD_ROLE_ID`) bypass the cooldown/cap and can escalate any
message immediately via the **Ask Historians** message context-menu action,
without waiting for the timer — this is also the fallback for anything the
three checks above miss.

## Topic of the Day
Posts an open-ended historical discussion prompt on a schedule — no correct
answer, just a reason for #history-general to talk. Topics are curated in
`historian_relay_bot/data/topics.json` (id/prompt/tag/era) and won't repeat
until the whole set has cycled through.

Config (env or `CONFIG_JSON`):
- `TOPIC_OF_DAY_ENABLED` (default `true`).
- `TOPIC_OF_DAY_CHANNEL_ID` (default unset = feature inert) — the channel to post in.
- `TOPIC_OF_DAY_HOUR_UTC` (default `14`) — the UTC hour after which the day's topic is posted.

Moderators can also post one immediately with `/topic_now [channel]`,
regardless of the schedule — handy for testing or for an impromptu topic.
