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
see `GUILD_ID` below). Moderators review each one in a private channel and,
once approved, it's posted into that topic's existing forum thread (e.g. a
"GEN" thread, an "EAST" thread, …) as a new message in the fixed post format,
with the original file re-uploaded under a standardized filename. No native
Discord forum tags are used — the topic is just the thread it's posted into
(and is also shown in the post's own `**Topic:**` line).

Flow:
1. `/submit` validates the input (reporting every problem at once if there
   are several) and checks the submitter isn't over their pending-submission
   cap or re-submitting a duplicate (same normalized author surname + main
   title).
2. On success, the submission is saved as `pending` and posted to
   `REVIEW_CHANNEL_ID` with a formatted preview, the file re-uploaded under
   its generated filename (if one was attached), and **Approve** / **Reject**
   / **Edit filename** buttons — restricted to `REVIEWER_ROLE_ID`.
3. **Approve** finds the existing forum thread for the submission's topic
   (see below) and posts the formatted content + file into it as a new
   message; **Reject** asks for a reason and DMs the submitter; **Edit
   filename** lets a reviewer tweak the author/short-title parts before
   approval.
4. Buttons keep working across restarts, and a submission can only be
   decided once (guarded with atomic status transitions).

A submission only ever posts into its **primary** topic's thread — if a
secondary topic is set, it's still shown in the post's `**Topic:** GEN ·
also EAST` line but isn't cross-posted into the second thread.

### Finding each topic's thread
For each topic code (`GEN`, `ORIG`, `WEST`, `EAST`, `MED`, `PAC`, `NAVAL`,
`AIR`, `ECON`, `OCC`, `DIPL`), the bot needs one destination forum thread
inside `SOURCES_FORUM_CHANNEL_ID`. It resolves this two ways:
1. `SOURCE_TOPIC_THREAD_IDS` (optional, most reliable) — explicit
   `CODE:threadid` pairs, e.g. `GEN:123456789012345678,EAST:234567890123456789`.
   Right-click a thread → Copy Thread ID (Developer Mode must be on).
2. Otherwise, it searches the forum's active + archived threads for one
   whose name contains that code as a whole word (case-insensitive) — so if
   you already have threads named things like "📚 GEN" or "Eastern Front
   (EAST)", no config is needed at all.

If a topic has no match (or more than one, which is ambiguous), the bot logs
a warning on startup, and Approve fails with a clear error telling the
reviewer what's missing rather than posting anywhere wrong.

### Config (env or `CONFIG_JSON`)
- `GUILD_ID` (required) — the guild `/submit` is registered to.
- `REVIEW_CHANNEL_ID` (required) — private text channel reviewers see submissions in.
- `SOURCES_FORUM_CHANNEL_ID` (required) — forum channel containing the per-topic threads.
- `REVIEWER_ROLE_ID` (required) — role allowed to Approve/Reject/Edit filename.
- `SOURCE_TOPIC_THREAD_IDS` (optional) — explicit topic → thread id mapping, see above.
- `MAX_PENDING_PER_USER` (default `3`) — per-user cap on simultaneous pending submissions.
- `ALLOWED_EXTENSIONS` (default `pdf,epub`) — allowed file extensions for the optional attachment.
- `SOURCE_TOPIC_PREFIX` (default `WW2-`) — prefix used only in generated filenames now (e.g.
  `WW2-ECON_Tooze_2006_...`); kept configurable in case other wars/eras get added later.

### Bot permissions needed
Send Messages, Send Messages in Threads, Attach Files, Embed Links, Read Message History (needed
to list archived threads when resolving a topic by name).

### Railway deployment note
SQLite (`DATABASE_PATH`) needs to live on a **mounted volume** — without one, the database (and
every pending/approved submission) is lost on every redeploy.
