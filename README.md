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

Config (env or `CONFIG_JSON`):
- `ASKHIST_WATCH_ENABLED` (default `true`) — feature on/off switch.
- `ASKHIST_WATCH_CHANNEL_IDS` (default empty = disabled until set) — CSV of channel IDs to watch, e.g. the `#history-general` channel ID.
- `ASKHIST_WATCH_MIN_WORDS` (default `4`) — a message needs a `?` and at least this many words to be tracked.
- `ASKHIST_WATCH_DELAY_SECONDS` (default `7200`, ~2 hours) — how long to wait before reminding.
- `ASKHIST_WATCH_PING_COOLDOWN_MINUTES` (default `60`) — per-user cooldown between "Ask Historians" triggers.
- `ASKHIST_WATCH_PING_MAX_PER_DAY` (default `3`) — per-user daily cap on "Ask Historians" triggers.

Moderators (`MOD_ROLE_ID`) bypass the cooldown/cap and can escalate any
message immediately via the **Ask Historians** message context-menu action,
without waiting for the timer.
