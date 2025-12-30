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

  last_asked_at INTEGER NOT NULL,
  daily_count INTEGER NOT NULL,
  daily_reset_at INTEGER NOT NULL,

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
