-- Operator ↔ Hermes chat panel — display history + unread bookkeeping.
--
-- Written by the dashboard container (which also runs the FastAPI startup
-- hook that first triggers run_migrations() from a non-cron process).
-- Read/written by both the FastAPI request handlers and the detached
-- chat_worker subprocess — chat_db.open_conn() sets busy_timeout for
-- three-writer coexistence.

CREATE TABLE chat_messages (
  message_id        INTEGER PRIMARY KEY,
  thread_id         TEXT NOT NULL,             -- 'default' in v1 (single operator)
  hermes_session_id TEXT,                       -- X-Hermes-Session-Id per thread
  ts                TEXT NOT NULL,              -- ISO UTC, seconds
  role              TEXT NOT NULL,              -- 'operator' | 'agent' | 'system' | 'tool'
  body              TEXT NOT NULL,
  hermes_run_id     TEXT,                       -- when SSE /v1/runs is used (v2)
  tool_call_json    TEXT,                       -- Hermes surfacing a proposed action
  status            TEXT NOT NULL DEFAULT 'delivered',
                                                -- 'processing' | 'delivered' | 'failed' | 'stream'
  error             TEXT
);

CREATE INDEX ix_chat_messages_thread_id ON chat_messages(thread_id, message_id DESC);

CREATE TABLE chat_read_state (
  thread_id            TEXT PRIMARY KEY,
  last_seen_message_id INTEGER NOT NULL
);
