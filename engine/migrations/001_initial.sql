CREATE TABLE IF NOT EXISTS schema_version (
  filename     TEXT PRIMARY KEY,
  applied_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
  event_uid         TEXT PRIMARY KEY,
  source            TEXT NOT NULL,
  source_kind       TEXT NOT NULL,
  name              TEXT NOT NULL,
  location          TEXT,
  date              TEXT NOT NULL,
  date_day          TEXT NOT NULL,
  description       TEXT,
  hosting_group     TEXT,
  source_url        TEXT,
  extra_json        TEXT,
  first_seen_at     TEXT NOT NULL,
  last_seen_at      TEXT NOT NULL,
  content_hash      TEXT NOT NULL,
  last_modified_at  TEXT NOT NULL,
  pushed_at         TEXT,
  pushed_hash       TEXT,
  status            TEXT NOT NULL DEFAULT 'active'
);

CREATE INDEX IF NOT EXISTS idx_events_date_day ON events(date_day);
CREATE INDEX IF NOT EXISTS idx_events_unpushed ON events(pushed_at) WHERE pushed_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_events_source ON events(source);

CREATE TABLE IF NOT EXISTS source_runs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  source      TEXT NOT NULL,
  started_at  TEXT NOT NULL,
  ended_at    TEXT,
  ok          INTEGER,
  fetched     INTEGER,
  inserted    INTEGER,
  updated     INTEGER,
  error       TEXT
);

CREATE INDEX IF NOT EXISTS idx_source_runs_source ON source_runs(source, started_at);
