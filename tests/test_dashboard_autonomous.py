"""The local dashboard is a supervised-mode surface: in an autonomous town
the TownLoops moderation queue is the only human gate, so the dashboard
refuses to start rather than offer local approvals that no longer apply."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def town(tmp_path, monkeypatch):
    """An isolated HERMES_HOME + town profile; returns a mode setter."""
    (tmp_path / "state").mkdir()
    profile = tmp_path / "town-profile.yaml"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("TOWNLOOPS_DB_PATH", str(tmp_path / "state" / "townloops.db"))
    monkeypatch.setenv("TOWN_PROFILE_PATH", str(profile))

    def mode(m: str) -> Path:
        profile.write_text(f"name: Test Town\noperating_mode: {m}\n")
        return profile
    return mode


def test_supervised_dashboard_starts(town):
    town("supervised")
    from dashboard import main
    with TestClient(main.app) as client:
        assert client.get("/login").status_code == 200


def test_autonomous_dashboard_refuses_to_start(town):
    town("autonomous")
    from dashboard import main
    with pytest.raises(RuntimeError, match="autonomous"):
        with TestClient(main.app):
            pass


def test_mode_is_read_at_startup_not_import(town):
    """Flipping a town to autonomous takes effect on the next start without
    a re-import (nothing caches the mode)."""
    town("supervised")
    from dashboard import main
    with TestClient(main.app):
        pass
    town("autonomous")
    with pytest.raises(RuntimeError):
        with TestClient(main.app):
            pass


def test_uvicorn_exits_nonzero_with_a_clear_message(town, tmp_path):
    town("autonomous")
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(
        p for p in (str(ROOT), os.environ.get("PYTHONPATH", "")) if p)}
    r = subprocess.run(
        [sys.executable, "-m", "uvicorn", "dashboard.main:app",
         "--host", "127.0.0.1", "--port", "0"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode != 0
    assert "operating_mode: autonomous" in r.stderr
    assert "TownLoops" in r.stderr
