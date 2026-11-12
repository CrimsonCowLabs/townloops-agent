-- 031_analyst_runs.sql
-- The daily analyst run's bookkeeping (#64, engine/analyst_runs) and the
-- cron-job outcome record its alerts come from (engine/job_health).
--
-- analyst_runs: one row per run, opened by `town-report budget start`.
--   status: 'running' | 'ok' | 'failed' | 'interrupted' (still running past
--   its max_minutes when the next run started).
--   budget_json: what the run was allowed ({max_minutes, max_firecrawl,
--   max_submissions}, town-profile.yaml `analyst:` over the defaults).
--   used_json: what it used, written at finish (minutes, firecrawl, submissions).
--   report_path: the run's daily report; backup_json: the pre-run backups.

CREATE TABLE analyst_runs (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at   TEXT NOT NULL,
  ended_at     TEXT,
  status       TEXT NOT NULL DEFAULT 'running',
  budget_json  TEXT NOT NULL,
  used_json    TEXT,
  report_path  TEXT,
  backup_json  TEXT,
  notes        TEXT
);

-- job_health: one row per scheduled job — its latest outcome and how many
-- runs in a row did not succeed. job: `cron:<hermes job id>` (read from
-- Hermes' cron output) or the skill's own name (a run that skipped on a lock).
--   last_outcome: 'ok' | 'failed' | 'skipped'; streak: consecutive non-ok
--   runs; alerted_at: set while a standing alert is out (cleared on ok).
CREATE TABLE job_health (
  job           TEXT PRIMARY KEY,
  last_run_at   TEXT NOT NULL,
  last_outcome  TEXT NOT NULL,
  streak        INTEGER NOT NULL DEFAULT 0,
  detail        TEXT,
  alerted_at    TEXT
);
