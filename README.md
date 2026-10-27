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

### Tests
`pytest` (added as a dev-only dependency) covers the `/submit` filename and post-format
pure functions:
```bash
pytest
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
`HISTORIANS_CHANNEL_ID` — then replies to confirm.

You can also ask the question first, then come back later and just **reply to
your own message with a mention** instead of retyping it:

```
[earlier] why did the Western Roman Empire fall when it did?
[later, as a reply to the above] @HistorianBot
```

The *original* message is what gets forwarded (with its own jump link), not
the reply — so a bare "@HistorianBot" works fine. Adding extra text in the
reply (e.g. "@HistorianBot can you also cover the economic angle?") carries
that through as additional context alongside the original question. This
only applies to replying to your *own* message — replying to someone else's
message and mentioning the bot forwards your own reply text instead (to
forward someone else's message, a moderator can use the context-menu action
below). Mentioning the bot with nothing to forward (no text in the mention,
and no resolvable original question) just gets a short nudge; nothing is
forwarded.

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
- `TOPIC_OF_DAY_USER_COOLDOWN_MINUTES` (default `10`) — per-user cooldown on `/topic` below.

Moderators can post one immediately with `/topic_now [channel]`, regardless
of the schedule — handy for testing or for an impromptu topic.

Any user can run **`/topic [tag] [era]`** (or the prefix-command equivalent
**`!topic [tag] [era]`**, e.g. `!topic religion ancient`) at any time to get
a discussion topic posted right in the channel they're in — this doesn't
touch the daily schedule or count toward it, it's just "give the channel
something to talk about." Optional `tag`/`era` filters narrow the pick to a
theme; a per-user cooldown (shared between both command forms) keeps it from
being spammed. Unlike the scheduled post, `/topic`/`!topic` results aren't
pinned.

## Book source submissions (/submit)
Members submit book sources with **`/submit`** (a guild-scoped slash command —
see `GUILD_ID` below), picking which historical **era** the source covers.
Moderators review each one in a private channel and, once approved, it's
posted into that era's existing forum thread as a new message in the fixed
post format, with the original file re-uploaded under a standardized
filename. No native Discord forum tags are used — the thread itself is the
category.

### Eras
| Era code | Label | Filename prefix |
|---|---|---|
| `BRONZE` | Bronze Age (c. 3000 BCE – c. 1050 BCE) | `BRONZE-` |
| `IRON` | Iron Age (c. 1050 BCE – c. 500 CE) | `IRON-` |
| `ARCHAIC` | Archaic Greece (776 BCE – 476 CE) | `ARCHAIC-` |
| `CLASSICAL` | Classical Antiquity (480 BCE – 476 CE) | `CLASSICAL-` |
| `MIGRATION` | Migration Period (300 – 700) | `MIGRATION-` |
| `MEDIEVAL` | Middle Ages (476 – 1453) | `MEDIEVAL-` |
| `EARLYMODERN` | Early modern period (1453 – 1789) | `EARLYMODERN-` |
| `LONG19` | Long nineteenth century (1789 – 1914) | `LONG19-` |
| `WORLDWARS` | World Wars (1914 – 1945) | `SOURCE_TOPIC_PREFIX` (default `WW2-`) |
| `COLDWAR` | Cold War (1945 – 1991) | `COLDWAR-` |
| `POSTMOD` | Postmodernity (1991 – present) | `POSTMOD-` |

The WW2 sub-topic dropdown (`GEN`, `EAST`, `ECON`, …) is available for every
era, not just `WORLDWARS` — it's entirely optional, shown in the post's
`**Topic:**` line whenever set, and never required or rejected based on era.
Add new eras in `ERA_DEFS` at the top of `cogs/book_sources.py`.

Flow:
1. `/submit` validates the input (reporting every problem at once if there
   are several) and checks the submitter isn't over their pending-submission
   cap or re-submitting a duplicate (same normalized author surname + main
   title).
2. On success, the submission is saved as `pending` and posted to
   `REVIEW_CHANNEL_ID` with a formatted preview, the file re-uploaded under
   its generated filename (if one was attached), and **Approve** / **Reject**
   / **Edit filename** buttons — restricted to `REVIEWER_ROLE_ID`.
3. **Approve** posts the formatted content + file into that era's thread
   (`ERA_THREAD_IDS`) as a new message; **Reject** asks for a reason and DMs
   the submitter; **Edit filename** lets a reviewer tweak the
   author/short-title parts before approval.
4. Buttons keep working across restarts, and a submission can only be
   decided once (guarded with atomic status transitions).

### Config (env or `CONFIG_JSON`)
- `GUILD_ID` (required) — the guild `/submit` is registered to.
- `REVIEW_CHANNEL_ID` (required) — private text channel reviewers see submissions in.
- `ERA_THREAD_IDS` (required) — one destination thread per era code, as `CODE:threadid` pairs
  separated by commas, e.g. `WORLDWARS:1553771786796011600,COLDWAR:...,MEDIEVAL:...` — all 11
  eras need an entry. Each value is the forum **post/thread itself** (not the parent forum
  channel). Developer Mode on → right-click the thread → Copy Thread ID.
- `REVIEWER_ROLE_ID` (required) — role allowed to Approve/Reject/Edit filename.
- `MAX_PENDING_PER_USER` (default `3`) — per-user cap on simultaneous pending submissions.
- `ALLOWED_EXTENSIONS` (default `pdf,epub`) — allowed file extensions for the optional attachment.
- `SOURCE_TOPIC_PREFIX` (default `WW2-`) — filename prefix for the `WORLDWARS` era specifically
  (e.g. `WW2-ECON_Tooze_2006_...`); every other era's prefix is fixed in `ERA_DEFS`.

If an `ERA_THREAD_IDS` entry is missing or misconfigured (e.g. set to the parent forum channel
instead of a specific post), the bot logs a specific, actionable warning on startup for that era,
and Approve fails with the same message instead of posting anywhere wrong.

### Bot permissions needed
Send Messages, Send Messages in Threads, Attach Files, Embed Links.

### Railway deployment note
SQLite (`DATABASE_PATH`) needs to live on a **mounted volume** — without one, the database (and
every pending/approved submission) is lost on every redeploy.
