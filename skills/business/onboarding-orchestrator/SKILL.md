---
name: onboarding-orchestrator
description: Workqueue drainer for the event-first pipeline. `tick` drains the event workqueue (claims pending per-event actions and invokes the matching skill) and emits a one-line JSON status. The old phase machine is retired; the legacy command names are kept for compatibility.
---

# onboarding-orchestrator

The historical phase machine (`bootstrapping → … → steady_state`) is gone. In the
event-first pipeline a "tick" means **drain the event workqueue**: for each
`next_action` (`extract-venue`, `extract-hosts`, `extract-people`,
`enrich-entities`, `ship`) claim a batch, invoke the matching skill with
`--event-uid`, and advance each event's `state`. See `engine/orchestrator.py`
(`drain_workqueue`) and `engine/event_state.py`.

Each tick:

1. Acquires `engine.lock.acquire("workqueue-drain")` — overlapping ticks skip
   rather than double-processing.
2. Reaps stale claims (rows a dead worker left claimed) so they can't stall.
3. Drains each action's queue (batch size 5), honoring per-action hourly rate
   limits from `town-profile.yaml`.
4. On skill success advances the event's state and enqueues the next action; on
   failure increments attempts and, at `MAX_ATTEMPTS`, marks the event `stuck-`.
5. Emits a one-line JSON status.

The real cron driver is `scripts/workqueue_heartbeat.py` (every 10 min); this
skill's `tick` is an equivalent entry point.

## Commands

```
$PYTHON $HERMES_HOME/skills/business/onboarding-orchestrator/orchestrate.py tick
$PYTHON $HERMES_HOME/skills/business/onboarding-orchestrator/orchestrate.py status
$PYTHON $HERMES_HOME/skills/business/onboarding-orchestrator/orchestrate.py finalize
```

- `tick` — drains the workqueue once. Exit 0 on success, 1 on lock contention.
- `status` — read-only snapshot (`agent_state` + workqueue counts).
- `finalize` — legacy no-op that returns the current status (kept for
  compatibility with old callers).

## Env

- `TOWNLOOPS_TOWN_SLUG`, `HERMES_HOME` — DB path + state discovery.
- `PYTHON` (optional) — override the interpreter used to shell out to
  skill CLIs. Defaults to `sys.executable`.
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` — optional; enables per-transition
  notification.

## Non-goals

- Not for on-demand skill invocation — call the skill CLIs directly.
- Not for cross-town coordination — one row of agent_state per town.
