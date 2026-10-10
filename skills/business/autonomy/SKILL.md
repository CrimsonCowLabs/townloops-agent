---
name: autonomy
description: Autonomous mode — explain any decision the agent made on its own (why an event was dropped or submitted, why a seed was retired, why submissions stopped), show the mode, gate and last 24h, and resume submissions after the circuit opened.
---

# autonomy

A town runs **supervised** (default — the operator approves seeds, events,
candidates and cancellations locally) or **autonomous** (`operating_mode:
autonomous` in town-profile.yaml — the agent makes those calls itself and the
TownLoops admin moderation queue is the only human gate). Logic:
`engine/autonomy.py`.

Every autonomous decision is recorded with a reason code and its evidence
(worker `autonomy` in the activity log). This skill reads them back.

## Usage

```bash
python autonomy/autonomy.py status                 # mode, knobs, circuit, pending, last 24h
python autonomy/autonomy.py why "Jazz Night"       # an event by title words or uid prefix
python autonomy/autonomy.py why 12                 # a seed by id (or by name)
python autonomy/autonomy.py why hermes-slt-1a2b    # a TownLoops slug we submitted
python autonomy/autonomy.py resume                 # close the circuit (operator only)
```

## Reason codes

- Events: `checks-passed`, `ai-verified` (approved); `past`, `beyond-horizon`,
  `no-start-time`, `not-an-event`, `out-of-town`, `series-duplicate`,
  `series-rejected-streak`, `insufficient-evidence`, `rejected-on-townloops`
  (dropped); `ai-unavailable` (deferred to the next run).
- Stuck events: `backoff` (wait), `backoff-elapsed` (retry),
  `retries-exhausted` (ship with what it has), `missing-required-fields`,
  `ship-failed`, `past` (drop).
- Seeds: `trial-yielded-events` (approve), `trial-not-found`,
  `trial-challenge`, `trial-empty-page`, `trial-empty-listing`,
  `trial-no-usable-events`, `trial-unreachable` (reject),
  `rejected-by-moderator`, `dead-source`, `health-alert-persists` (retire, or
  `calendar-moved` when a working calendar was found on the same site);
  `links-off-site` / `links-on-site` (syndicator / primary).
- Submissions: `moderator-published`, `moderator-rejected`, `moderator-merged`,
  `moderator-gone`. Changes: `published-value-changed` (a date/venue change
  submitted for merge). Cancellations: `page-states-cancelled`.
- Circuit: `published-without-review` (the API key can publish — switch to an
  `api_ingest` key), `rejection-rate`.

`resume` is the operator's call: fix the cause first. The circuit never closes
itself.
