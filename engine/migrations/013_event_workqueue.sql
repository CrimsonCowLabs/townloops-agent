-- 013_event_workqueue.sql
-- The cyclical state machine. Each event has 0..1 active workqueue row driving
-- it to the next state. Workers claim rows via UPDATE with a WHERE-clause
-- on old state to avoid double-processing.

CREATE TABLE IF NOT EXISTS event_workqueue (
  queue_id          INTEGER PRIMARY KEY AUTOINCREMENT,
  event_uid         TEXT NOT NULL,                    -- FK to events.event_uid
  next_action       TEXT NOT NULL,                    -- 'extract-metadata' | 'extract-venue' | 'extract-hosts' | 'extract-people' | 'enrich-entities' | 'ship'
  attempts          INTEGER NOT NULL DEFAULT 0,
  last_error        TEXT,
  last_attempt_at   TEXT,
  enqueued_at       TEXT NOT NULL,
  claimed_by        TEXT,
  claimed_at        TEXT,
  resolved_at       TEXT,
  UNIQUE (event_uid, next_action)
);

CREATE INDEX IF NOT EXISTS idx_workqueue_pending
    ON event_workqueue(next_action, enqueued_at)
    WHERE resolved_at IS NULL AND (claimed_at IS NULL OR attempts > 0);
CREATE INDEX IF NOT EXISTS idx_workqueue_event ON event_workqueue(event_uid);


-- inspection.py records what the agent is currently working on
CREATE TABLE IF NOT EXISTS agent_activity (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  ts            TEXT NOT NULL,
  worker        TEXT,                                  -- skill name or 'orchestrator'
  action        TEXT NOT NULL,                         -- 'llm-call' | 'firecrawl' | 'sql-mutation' | 'skill-invoke'
  target        TEXT,                                  -- event_uid or URL or query
  detail        TEXT,                                  -- first 200 chars of prompt/URL
  duration_ms   INTEGER,
  ok            INTEGER,                               -- 0 or 1
  error         TEXT
);

CREATE INDEX IF NOT EXISTS idx_activity_ts ON agent_activity(ts DESC);
