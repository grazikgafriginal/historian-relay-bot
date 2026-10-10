# Historians of the House Team Bot (discord.py 2.x)

A Discord bot for history servers:
- Users submit questions via `/askhist` in approved channels
- Bot posts a visible origin message + optional thread
- Bot forwards a workflow embed to `#verified-historians` or (optional) a mod queue
- Verified historians/mods can Claim / Unclaim / Needs Context / Close / Publish Answer (reply-based or modal)
- Approved answers are reposted to the original thread (preferred) or origin channel
- SQLite persistence + restart recovery (views reattached)
- Forwards a message to the Historians when the bot is @-mentioned (see below)

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

## Ask Historians via mention
@-mention the bot together with a question anywhere in the server and it's
forwarded to the Historians immediately — no waiting, no in-channel reminder,
no DM. The mention itself is the explicit signal; there's no passive
channel-watching or heuristic question-detection to get wrong.

```
@HistorianBot why did the Western Roman Empire fall when it did?
```

The bot strips its own mention from the text, creates a tracking record, and
posts a Historian Request pinging `VERIFIED_HISTORIAN_ROLE_ID` in
`HISTORIANS_CHANNEL_ID` — then replies to confirm. Mentioning the bot with no
question text just gets a short "mention me together with your question"
nudge; nothing is forwarded.

When a Historian replies (to the original message or to the Historian
Request), the request is marked resolved and the response is logged for
future KPI reporting — nothing is ever shown publicly. A "✅ Mark Resolved"
button on the request itself is a manual fallback.

Moderators (`MOD_ROLE_ID`) can also forward *any* message via the
**Ask Historians** message context-menu action (right-click a message →
Apps → Ask Historians) — useful for a question that wasn't addressed to the
bot, or where the asker forgot to mention it. Moderators bypass the
cooldown/cap below.

Anti-spam, so the Historian role can't be repeatedly pinged:
- A given question can only be forwarded once (further mentions/escalation attempts on an already-escalated question are rejected).
- Per-user cooldown + daily cap on triggering a forward.

Config (env or `CONFIG_JSON`):
- `ASKHIST_MENTION_ENABLED` (default `true`) — feature on/off switch.
- `ASKHIST_MENTION_COOLDOWN_MINUTES` (default `60`) — per-user cooldown between forwards triggered by mention.
- `ASKHIST_MENTION_MAX_PER_DAY` (default `3`) — per-user daily cap on forwards triggered by mention.

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
