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
  PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS guessyear_channel_categories (
  guild_id TEXT NOT NULL,
  channel_id TEXT NOT NULL,
  categories_json TEXT NOT NULL DEFAULT '[]',
  PRIMARY KEY (guild_id, channel_id)
);
