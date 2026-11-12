# Cron rhythm

What runs when for a town (the schedule itself is `cron/jobs.json`; trust it over this table if they disagree). Read this when the operator asks when something runs or why a job did or didn't fire.

| When | Job | LLM? |
|---|---|---|
| Every 10 min | Workqueue drain (derive/enrich/ship the approved events) | no* |
| Every 15 min | Ship operator-approved business candidates (`business-discovery` promote, ≤3) | no* |
| Every 20 min | Performer/creator research (`performer-research`, ≤3) | no |
| Every 30 min | Mark operator-confirmed cancellations on TownLoops (autonomous: submit page-evidenced ones for merge) | no |
| 06:00 & 18:00 daily | Event-find over approved seeds | yes |
| 03:30 daily | Entity live sync (resync, fill live blanks, verify; adopt Ingest Refs on pre-ref rows; waits out a running drain) | no |
| 04:15 daily | TownLoops API snapshot + breaking-change check | no |
| 04:15 daily | Seed hunt — web search, known organisations' calendars, sites events link to (weekly also model memory); ≤40 Firecrawl calls, new seeds proposed | weekly |
| 04:30 daily | Feed probe on never-probed seeds | no |
| 04:45 daily | Seed review — autonomous: assign feed-matched adapters, trial proposed seeds, classify, retire/relocate; supervised: only propose adapters | yes |
| hourly :05 | Moderation feedback — autonomous only: read back TownLoops outcomes | no |
| 03:07 daily | Organisation harvest — autonomous only: businesses, communities, non-profits, venues on rotating gap-driven terms; sweep, verify, approve (off the 15-min promote's minute; waits out a running promote) | no |
| 08:00–10:59 daily (per town) | **Analyst run** (`town-analyst`): a budgeted LLM session — review yesterday, hunt + trial sources, event-find, resolve holds, harvest organisations, live sync + enrichment, one drain, the daily report (`state/reports/<date>-analyst.md` + Telegram). Time: `analyst.schedule`, else derived from the slug by `bin/townloops-agent-add`. On demand: Telegram `/town_analyst`, or `hermes cron run town-analyst` | **agent** |
| 21:00 daily | Nightly digest (events by state, workqueue depth, seeds) | no |
| 02:00 Sunday | Weekly reconcile against seeds + live | yes |
| 08:00 Sunday | Newsletter compose (draft) | yes |
| 20:00 Sunday | Newsletter push reminder | yes |
| 10:00 Monday | Sponsor placement audit | yes |

All jobs except the analyst run and the newsletter and sponsor digests run as raw subprocesses (`no_agent: true`) — no LLM turn per invocation; the analyst run is a full Hermes agent turn with its tools. "yes" means the script calls the Codex CLI directly (extraction, research); *enrichment inside the drain/promote can call it too when a category needs classifying. Human-facing digests (newsletter, sponsor audit) route through the LLM.
