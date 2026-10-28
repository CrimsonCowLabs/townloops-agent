# Cron rhythm

What runs when for a town (the schedule itself is `cron/jobs.json`; trust it over this table if they disagree). Read this when the operator asks when something runs or why a job did or didn't fire.

| When | Job | LLM? |
|---|---|---|
| Every 10 min | Workqueue drain (derive/enrich/ship the approved events) | no* |
| Every 15 min | Ship operator-approved business candidates (`business-discovery` promote, ≤3) | no* |
| Every 20 min | Performer/creator research (`performer-research`, ≤3) | no |
| Every 30 min | Mark operator-confirmed cancellations on TownLoops (autonomous: submit page-evidenced ones for merge) | no |
| 06:00 & 18:00 daily | Event-find over approved seeds | yes |
| 03:30 daily | Entity live sync (resync, fill live blanks, verify) | no |
| 04:15 daily | TownLoops API snapshot + breaking-change check | no |
| 04:30 daily | Feed probe on never-probed seeds | no |
| 04:45 daily | Seed review — autonomous: assign feed-matched adapters, trial proposed seeds, classify, retire/relocate; supervised: only propose adapters | yes |
| hourly :05 | Moderation feedback — autonomous only: read back TownLoops outcomes | no |
| 03:00 Wednesday | Organisation harvest — autonomous only: sweep, verify, approve | no |
| 21:00 daily | Nightly digest (events by state, workqueue depth, seeds) | no |
| 02:00 Sunday | Weekly reconcile against seeds + live | yes |
| 04:00 Sunday | Event-seed discovery | yes |
| 08:00 Sunday | Newsletter compose (draft) | yes |
| 20:00 Sunday | Newsletter push reminder | yes |
| 10:00 Monday | Sponsor placement audit | yes |

All jobs except the newsletter and sponsor digests run as raw subprocesses (`no_agent: true`) — no LLM turn per invocation. "yes" means the script calls the Codex CLI directly (extraction, research); *enrichment inside the drain/promote can call it too when a category needs classifying. Human-facing digests (newsletter, sponsor audit) route through the LLM.
