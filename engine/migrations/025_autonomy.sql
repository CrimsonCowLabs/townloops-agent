-- 025_autonomy.sql
-- Autonomous mode (engine/autonomy.py, engine/moderation.py): the agent
-- makes the local operator's calls and TownLoops moderation is the only
-- human gate.
--
-- submissions: every record the agent created on TownLoops in autonomous
-- mode, and what the TownLoops moderator did with it. kind: 'new' | 'change'
-- (a date/venue change to a published event, for the merge flow) |
-- 'cancellation'. target_slug: the published event a change or a
-- cancellation should merge into. seed / series_slug / discovery_source:
-- what the outcome is attributed to. status: 'pending' | 'published' |
-- 'rejected' | 'merged' (into surviving_slug) | 'gone' (deleted or merged
-- into a record we couldn't find). published_unreviewed = 1 when the create
-- came back already published (the circuit opened on it).
--
-- autonomy_state: key/value — 'circuit' holds a JSON {reason, evidence, at}
-- while submissions are stopped, absent = closed.
--
-- event_seeds.trust_score: engine/autonomy.seed_trust, refreshed by the
-- seed review and the moderation poll. status_note / status_changed_at:
-- why the seed was approved, rejected or retired (status 'retired' is
-- reversible — the restore subcommand). previous_url: the URL a moved
-- calendar replaced. trial_at: when a proposed seed was last dry-run.

CREATE TABLE IF NOT EXISTS submissions (
  id                    INTEGER PRIMARY KEY AUTOINCREMENT,
  resource              TEXT NOT NULL,
  slug                  TEXT NOT NULL,
  local_uid             TEXT,
  kind                  TEXT NOT NULL DEFAULT 'new',
  target_slug           TEXT,
  seed                  TEXT,
  series_slug           TEXT,
  discovery_source      TEXT,
  status                TEXT NOT NULL DEFAULT 'pending',
  published_unreviewed  INTEGER NOT NULL DEFAULT 0,
  submitted_at          TEXT NOT NULL,
  checked_at            TEXT,
  decided_at            TEXT,
  surviving_slug        TEXT,
  UNIQUE (resource, slug)
);
CREATE INDEX IF NOT EXISTS idx_submissions_status ON submissions(status, checked_at);
CREATE INDEX IF NOT EXISTS idx_submissions_seed ON submissions(seed);
CREATE INDEX IF NOT EXISTS idx_submissions_series ON submissions(series_slug);

CREATE TABLE IF NOT EXISTS autonomy_state (
  key         TEXT PRIMARY KEY,
  value       TEXT,
  updated_at  TEXT NOT NULL
);

ALTER TABLE event_seeds ADD COLUMN trust_score REAL;
ALTER TABLE event_seeds ADD COLUMN status_note TEXT;
ALTER TABLE event_seeds ADD COLUMN status_changed_at TEXT;
ALTER TABLE event_seeds ADD COLUMN previous_url TEXT;
ALTER TABLE event_seeds ADD COLUMN trial_at TEXT;

CREATE INDEX IF NOT EXISTS idx_activity_worker_target ON agent_activity(worker, target);
