#!/usr/bin/env bash
# Cron wrapper: event-ingest ingest + ship.
# Invoked by event-collect-am/pm cron jobs.
set -euo pipefail

SKILL=/home/agent/.hermes/skills/business/event-ingest/ingest.py
/usr/bin/python3 "$SKILL" ingest
/usr/bin/python3 "$SKILL" ship
