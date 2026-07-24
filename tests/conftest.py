"""Shared pytest fixtures.

Notes on env:
  Every module under `engine/` and `skills/` reads `TOWNLOOPS_TOWN_SLUG`,
  `TOWNLOOPS_API_URL`, etc. at import time. Tests need those set BEFORE any
  fixture or test body triggers an import, so we monkey-patch os.environ
  here at collection time (via `pytest_configure`) rather than as a fixture.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest


def pytest_configure(config):
    """Ensure env vars every module reads at import time are set.

    Tests shouldn't have to remember to set these — set defaults here and
    tests override via monkeypatch when a specific value matters.
    """
    os.environ.setdefault("TOWNLOOPS_TOWN_SLUG", "test-town")
    os.environ.setdefault("TOWNLOOPS_API_URL", "http://test.invalid:8004")
    os.environ.setdefault("TOWNLOOPS_API_KEY", "test-key-invalid")
    # AGENT_TZ is needed for ingest_core.DEFAULT_TZ to resolve deterministically.
    os.environ.setdefault("AGENT_TZ", "America/Los_Angeles")


@pytest.fixture
def tmp_state_dir(tmp_path, monkeypatch):
    """Isolated state dir under $TOWNLOOPS_LOCK_DIR + $HERMES_HOME.

    Redirects everything that would touch shared state (locks, DB paths,
    categories snapshot). Yields the path so tests can seed files.
    """
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("TOWNLOOPS_LOCK_DIR", str(state / "locks"))
    monkeypatch.setenv("TOWNLOOPS_DB_PATH", str(state / "townloops.db"))
    yield state


@pytest.fixture
def fresh_db(tmp_state_dir):
    """SQLite connection to an empty DB with all migrations applied.

    Uses engine.db.run_migrations so the schema is byte-identical to prod.
    Sidesteps the module-level DEFAULT_DB_PATH cache by connecting to an
    explicit path.
    """
    from engine import db as db_mod

    db_path = tmp_state_dir / "townloops.db"
    conn = db_mod.connect(str(db_path))
    db_mod.run_migrations(conn)
    yield conn
    conn.close()


@pytest.fixture
def sample_event():
    """A representative Event object for identity/hashing tests."""
    from engine import ingest_core as core
    return core.Event(
        name="Jazz Night",
        location="Riva Grill, South Lake Tahoe, CA",
        date="2026-07-15T20:00:00-07:00",
        description="Live music at Riva Grill.",
        hosting_group="SLT Jazz",
        source="test-source",
        source_url="https://example.com/jazz",
        venue_name="Riva Grill",
        end_at="2026-07-15T23:00:00-07:00",
        category_slug="live-music",
    )
