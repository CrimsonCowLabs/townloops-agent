---
name: autonomy
description: Autonomous mode — explain any decision the agent made on its own (why an event was dropped or submitted, why a seed was retired, why submissions stopped), show the mode, gate and last 24h, and resume submissions after the circuit opened.
---

# autonomy

A town runs **autonomous** (`operating_mode: autonomous` in town-profile.yaml,
the default for a new town — the agent makes the calls itself and the
TownLoops admin moderation queue is the only human gate) or **supervised**
(a profile without the key — the operator approves seeds, events, candidates
locally). Cancellations are operator-confirmed in both. Logic:
`engine/autonomy.py`.

Every autonomous decision is recorded with a reason code and its evidence
(worker `autonomy` in the activity log). This skill reads them back.

## Usage

```bash
python autonomy/autonomy.py status                 # mode, knobs, circuit, pending + cap, last 24h
python autonomy/autonomy.py why "Jazz Night"       # an event by title words or uid prefix
python autonomy/autonomy.py why 12                 # a seed by id (or by name)
python autonomy/autonomy.py why hermes-slt-1a2b    # a TownLoops slug we submitted
python autonomy/autonomy.py resume                 # close the circuit (operator only)
python autonomy/autonomy.py accept 3f2a9c           # ship an event held for a source mismatch or implausible time as is
python autonomy/autonomy.py decline 3f2a9c          # or decline it (held events only)
```

`accept`/`decline` are the operator's, and the daily analyst run's
(`town-analyst`) once it has re-read the event's page and the page settles
it — with `--note` saying what did. The 10-minute pipeline never decides a
hold. The pending cap is `autonomy.max_pending` when set, else max(100, 3 ×
what the moderator decided in the last 7 days).

## Reason codes

- Events: `checks-passed`, `series-established`, `ai-verified` (approved); `past`, `beyond-horizon`,
  `no-start-time`, `not-an-event`, `out-of-town`, `series-duplicate`,
  `series-rejected-streak`, `insufficient-evidence`, `rejected-on-townloops`
  (dropped); `ai-unavailable` (deferred to the next run).
- Stuck events: `backoff` (wait), `backoff-elapsed` (retry),
  `retries-exhausted` (ship with what it has), `missing-required-fields`,
  `ship-failed`, `past` (drop).
- Seeds: `trial-yielded-events`, `trial-many-upcoming` (approve), `trial-not-found`,
  `trial-challenge`, `trial-empty-page`, `trial-empty-listing`,
  `trial-no-usable-events`, `trial-low-yield`, `trial-unreachable` (reject),
  `low-trust`, `dead-source`, `health-alert-persists` (retire, or
  `calendar-moved` when a working calendar was found on the same site);
  `low-trust` / `links-off-site` (syndicator), `links-on-site` (primary).
  Seed decisions carry the trust score (`trust`) and the threshold applied.
- Submissions: `moderator-published`, `moderator-rejected`, `moderator-merged`,
  `moderator-gone`. Changes: `published-value-changed` (a date/venue change
  submitted for merge). Cancellations: `page-states-cancelled`.
- Provenance: `provenance-mismatch` (hold) — the listing card or detail page
  doesn't vouch for the event (#45). It waits for `accept` / `decline`
  (listed in every nightly digest and daily report).
- Times: `implausible-time` (hold) — a pre-dawn start, an end before its
  start or a duration over the limit (engine/time_plausibility, #46). Same
  contract: the operator's `accept` ships it with those times (a real 05:30
  race start), or `decline`.
- Circuit: `published-without-review` (the API key can publish — switch to an
  `api_ingest` key), `rejection-rate`.

`resume` is the operator's call: fix the cause first. The circuit never closes
itself.
