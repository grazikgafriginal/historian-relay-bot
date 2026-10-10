PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS questions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,

  guild_id TEXT NOT NULL,
  created_by_user_id TEXT NOT NULL,

  question_text TEXT NOT NULL,
  tag TEXT,
  era TEXT,

  status TEXT NOT NULL, -- pending, queued, claimed, needs_context, answered, closed, denied, cancelled

  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,

  origin_channel_id TEXT NOT NULL,
  origin_message_id TEXT NOT NULL,
  origin_thread_id TEXT,

  queue_message_id TEXT,
  hist_message_id TEXT,

  claimed_by_user_id TEXT,
  claimed_at INTEGER,

  answer_text TEXT,
  answered_by_user_id TEXT,
  answered_at INTEGER
);

CREATE INDEX IF NOT EXISTS idx_questions_guild_status ON questions(guild_id, status);
CREATE INDEX IF NOT EXISTS idx_questions_origin ON questions(guild_id, origin_channel_id, origin_message_id);
CREATE INDEX IF NOT EXISTS idx_questions_hist_msg ON questions(guild_id, hist_message_id);
CREATE INDEX IF NOT EXISTS idx_questions_queue_msg ON questions(guild_id, queue_message_id);

CREATE TABLE IF NOT EXISTS cooldowns (
  guild_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  last_asked_at INTEGER NOT NULL DEFAULT 0,
  daily_count INTEGER NOT NULL DEFAULT 0,
  daily_reset_at INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS blacklist (
  guild_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  reason TEXT,
  added_by_user_id TEXT NOT NULL,
  added_at INTEGER NOT NULL,
  PRIMARY KEY (guild_id, user_id)
);

-- -------------------------
-- Guess the Year (mini-game)
-- -------------------------

CREATE TABLE IF NOT EXISTS guessyear_rounds (
  round_id INTEGER PRIMARY KEY AUTOINCREMENT,
  guild_id TEXT NOT NULL,
  channel_id TEXT NOT NULL,
  started_by_user_id TEXT NOT NULL,
  event_id TEXT NOT NULL,
  correct_year INTEGER NOT NULL,
  started_at INTEGER NOT NULL,
  ends_at INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'active', -- active, ended, cancelled
  hints_used INTEGER NOT NULL DEFAULT 0
);

-- Enforce at most one ACTIVE round per channel, but allow historical rounds.
CREATE UNIQUE INDEX IF NOT EXISTS idx_guessyear_active_round
  ON guessyear_rounds(guild_id, channel_id)
  WHERE status='active';

CREATE INDEX IF NOT EXISTS idx_guessyear_rounds_active
  ON guessyear_rounds(guild_id, channel_id, status, ends_at);

CREATE TABLE IF NOT EXISTS guessyear_guesses (
  round_id INTEGER NOT NULL,
  user_id TEXT NOT NULL,
  guess_year INTEGER NOT NULL,
  guessed_at INTEGER NOT NULL,
  PRIMARY KEY (round_id, user_id),
  FOREIGN KEY (round_id) REFERENCES guessyear_rounds(round_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS guessyear_stats (
  guild_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  wins INTEGER NOT NULL DEFAULT 0,
  plays INTEGER NOT NULL DEFAULT 0,
  exact_hits INTEGER NOT NULL DEFAULT 0,
  current_streak INTEGER NOT NULL DEFAULT 0,
  best_streak INTEGER NOT NULL DEFAULT 0,
  total_distance INTEGER NOT NULL DEFAULT 0,
  duel_wins INTEGER NOT NULL DEFAULT 0,
  duel_losses INTEGER NOT NULL DEFAULT 0,
  xp INTEGER NOT NULL DEFAULT 0,
  last_played_at INTEGER NOT NULL DEFAULT 0,
  daily_streak INTEGER NOT NULL DEFAULT 0,
  daily_streak_date TEXT NOT NULL DEFAULT '',
  weekly_xp INTEGER NOT NULL DEFAULT 0,
  weekly_wins INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS guessyear_channel_categories (
  guild_id TEXT NOT NULL,
  channel_id TEXT NOT NULL,
  categories_json TEXT NOT NULL DEFAULT '[]',
  PRIMARY KEY (guild_id, channel_id)
);

CREATE TABLE IF NOT EXISTS guessyear_duel_matchups (
  guild_id TEXT NOT NULL,
  user_a TEXT NOT NULL,
  user_b TEXT NOT NULL,
  wins_a INTEGER NOT NULL DEFAULT 0,
  wins_b INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (guild_id, user_a, user_b)
);

CREATE TABLE IF NOT EXISTS guessyear_achievements (
  guild_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  achievement_key TEXT NOT NULL,
  earned_at INTEGER NOT NULL,
  PRIMARY KEY (guild_id, user_id, achievement_key)
);

CREATE TABLE IF NOT EXISTS guessyear_daily (
  guild_id TEXT NOT NULL,
  date_key TEXT NOT NULL,
  event_id TEXT NOT NULL,
  correct_year INTEGER NOT NULL,
  message_id TEXT,
  PRIMARY KEY (guild_id, date_key)
);

CREATE TABLE IF NOT EXISTS guessyear_daily_guesses (
  guild_id TEXT NOT NULL,
  date_key TEXT NOT NULL,
  user_id TEXT NOT NULL,
  guess_year INTEGER NOT NULL,
  guessed_at INTEGER NOT NULL,
  PRIMARY KEY (guild_id, date_key, user_id)
);

CREATE TABLE IF NOT EXISTS guessyear_referrals (
  guild_id TEXT NOT NULL,
  invited_user_id TEXT NOT NULL,
  inviter_user_id TEXT NOT NULL,
  joined_at INTEGER NOT NULL,
  rewarded INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (guild_id, invited_user_id)
);

CREATE TABLE IF NOT EXISTS bot_dm_optout (
  user_id TEXT PRIMARY KEY,
  opted_out INTEGER NOT NULL DEFAULT 0,
  updated_at INTEGER NOT NULL
);

-- -------------------------
-- Ask Historians forwarding (triggered by @mentioning the bot, or by a
-- moderator's "Ask Historians" context-menu action — no more passive
-- channel-watching/timer/reminder flow).
-- -------------------------

-- Replaces the old askhist_watch table (dropped below): that table's
-- watching/reminded states and reminder_* columns no longer apply now that
-- forwarding happens immediately on an explicit trigger instead of after an
-- unanswered-question timer.
DROP TABLE IF EXISTS askhist_watch;

CREATE TABLE IF NOT EXISTS askhist_forward (
  id INTEGER PRIMARY KEY AUTOINCREMENT,

  guild_id TEXT NOT NULL,
  channel_id TEXT NOT NULL,
  message_id TEXT NOT NULL,
  author_id TEXT NOT NULL,
  question_text TEXT NOT NULL,

  created_at INTEGER NOT NULL,

  -- new, escalated, resolved
  status TEXT NOT NULL DEFAULT 'new',

  escalated INTEGER NOT NULL DEFAULT 0,
  escalated_by_user_id TEXT,
  escalated_at INTEGER,
  historian_request_message_id TEXT,

  resolved INTEGER NOT NULL DEFAULT 0,
  resolved_by_user_id TEXT,
  resolved_at INTEGER,

  updated_at INTEGER NOT NULL,

  UNIQUE (guild_id, message_id)
);

CREATE INDEX IF NOT EXISTS idx_askhist_forward_guild_status ON askhist_forward(guild_id, status);
CREATE INDEX IF NOT EXISTS idx_askhist_forward_request_msg ON askhist_forward(guild_id, historian_request_message_id);

-- Anti-spam cooldown for triggering an "Ask Historians" forward (by mention
-- or by pressing a button); shared across trigger types.
CREATE TABLE IF NOT EXISTS historian_ping_cooldowns (
  guild_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  last_ping_at INTEGER NOT NULL DEFAULT 0,
  daily_count INTEGER NOT NULL DEFAULT 0,
  daily_reset_at INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (guild_id, user_id)
);

-- Internal KPI tracking: one row per Historian response to a forwarded/escalated question.
CREATE TABLE IF NOT EXISTS historian_responses (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  guild_id TEXT NOT NULL,
  watch_id INTEGER,
  question_message_id TEXT NOT NULL,
  historian_user_id TEXT NOT NULL,
  response_message_id TEXT NOT NULL,
  responded_at INTEGER NOT NULL,
  after_official_request INTEGER NOT NULL DEFAULT 0,
  UNIQUE (guild_id, watch_id, historian_user_id, response_message_id)
);

CREATE INDEX IF NOT EXISTS idx_historian_responses_guild_user ON historian_responses(guild_id, historian_user_id);
CREATE INDEX IF NOT EXISTS idx_historian_responses_watch ON historian_responses(guild_id, watch_id);

-- -------------------------
-- Topic of the Day
-- -------------------------

-- Append-only log: doubles as the "already posted automatically today" guard
-- (auto=1 rows) and the no-repeat-until-the-pool-cycles history for topic selection.
CREATE TABLE IF NOT EXISTS topic_of_day_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  guild_id TEXT NOT NULL,
  channel_id TEXT NOT NULL,
  topic_id TEXT NOT NULL,
  message_id TEXT,
  posted_at INTEGER NOT NULL,
  date_key TEXT NOT NULL,
  auto INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_topic_of_day_log_guild_date ON topic_of_day_log(guild_id, date_key);
CREATE INDEX IF NOT EXISTS idx_topic_of_day_log_guild_posted ON topic_of_day_log(guild_id, posted_at);
