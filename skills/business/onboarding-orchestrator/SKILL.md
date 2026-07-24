---
name: onboarding-orchestrator
description: Per-town autonomous loop driver. `tick` runs one bounded phase step and emits a one-line JSON status; the cron entry `onboarding-tick` calls it hourly with `no_agent: true` so there's no LLM turn per tick. Phase state persists in state/townloops.db.agent_state and is a hard singleton.
---

# onboarding-orchestrator

Drives the phase machine documented in `docs/STATUS.md` and
`plans/rosy-munching-milner.md`:

```
bootstrapping → discovering → verifying → researching
             → enriching → shipping → steady_state
```

Each tick:

1. Acquires `engine.lock.acquire("orchestrator")` — a second concurrent tick
   errors out rather than double-transitioning.
2. Loads (or initializes) `agent_state`.
3. Runs the current phase's handler — which shells out to the appropriate
   skill CLIs (business-discovery, community-discovery, entity-research,
   {venue,business,community,performer,creator}-enrichment, event-ingest).
4. Updates tick counters. Advances phase when either the phase's hard
   predicate is satisfied OR the quiet-tick counter reaches 3 (for phases
   that advance on "nothing new" rather than "nothing left").
5. Force-advances after 168 consecutive ticks in the same phase (~7 days
   hourly), with a Telegram warning.
6. Emits a one-line JSON status.

## Commands

```
python skills/business/onboarding-orchestrator/orchestrate.py tick
python skills/business/onboarding-orchestrator/orchestrate.py status
python skills/business/onboarding-orchestrator/orchestrate.py finalize
```

- `tick` — the cron entry point. Does one bounded step. Exit 0 on success,
  1 on lock contention.
- `status` — read-only dump of `agent_state`.
- `finalize` — force `phase='steady_state'`. Useful for testing failover
  or skipping onboarding on a town that's already been populated by
  another means.

## Env

- `TOWNLOOPS_TOWN_SLUG`, `HERMES_HOME` — DB path + state discovery.
- `PYTHON` (optional) — override the interpreter used to shell out to
  skill CLIs. Defaults to `sys.executable`.
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` — optional; enables per-transition
  notification.

## Non-goals

- Not for on-demand skill invocation — call the skill CLIs directly.
- Not for cross-town coordination — one row of agent_state per town.
