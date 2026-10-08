"""event-reconcile under cron: the detached run's failures reach the operator
(dashboard ticker + Telegram), not only its log file."""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _activity(db: Path) -> list[tuple]:
    conn = sqlite3.connect(db)
    try:
        return conn.execute(
            "SELECT action, ok, detail, error FROM agent_activity WHERE worker = 'event-reconcile'"
        ).fetchall()
    finally:
        conn.close()


@pytest.fixture
def telegrams(monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr("engine.orchestrator._telegram_notify", sent.append)
    return sent


@pytest.fixture
def wrapper(fresh_db, tmp_state_dir, monkeypatch, tmp_path):
    mod = _load(ROOT / "scripts/event_reconcile.py", "event_reconcile_cron")
    monkeypatch.setattr("engine.inspection._DB_PATH", tmp_state_dir / "townloops.db")
    monkeypatch.setenv("PYTHON", sys.executable)
    monkeypatch.setenv("TOWNLOOPS_TOWN_SLUG", "test-town")

    def fake_skill(body: str) -> Path:
        path = tmp_path / "fake_reconcile.py"
        path.write_text(body)
        monkeypatch.setattr(mod, "SKILL", path)
        return path

    mod.fake_skill = fake_skill
    return mod


class TestDetach:
    def test_cron_call_detaches_a_supervisor_and_returns(self, wrapper, monkeypatch, tmp_state_dir):
        wrapper.fake_skill("")
        spawned = []

        class FakePopen:
            pid = 4242

            def __init__(self, cmd, **kw):
                spawned.append((cmd, kw))

        monkeypatch.setattr(wrapper.subprocess, "Popen", FakePopen)
        assert wrapper.main(["--max-lookups", "10"]) == 0
        [(cmd, kw)] = spawned
        assert kw["start_new_session"] is True
        assert cmd[2] == "--supervise"
        assert Path(cmd[3]).parent == tmp_state_dir / "logs"
        assert cmd[4:] == ["--max-lookups", "10"]


class TestSupervisedRun:
    def test_failed_run_reaches_dashboard_and_telegram(self, wrapper, tmp_state_dir, telegrams):
        wrapper.fake_skill(
            "import sys\n"
            "print('{\"seed\": \"x\"}')\n"
            "print('engine.townloops_api.ApiError: 401 token expired', file=sys.stderr)\n"
            "sys.exit(1)\n")
        log = tmp_state_dir / "logs" / "event-reconcile-test.log"
        log.parent.mkdir()

        assert wrapper.supervise(log, []) == 1

        [(action, ok, _, error)] = _activity(tmp_state_dir / "townloops.db")
        assert (action, ok) == ("run-failed", 0)
        assert "401 token expired" in error
        [msg] = telegrams
        assert "test-town" in msg and "reconcile failed" in msg and "401 token expired" in msg
        assert log.name in msg

    def test_killed_run_is_reported(self, wrapper, tmp_state_dir, telegrams):
        wrapper.fake_skill("import os, signal\nos.kill(os.getpid(), signal.SIGKILL)\n")
        log = tmp_state_dir / "logs" / "event-reconcile-test.log"
        log.parent.mkdir()

        wrapper.supervise(log, [])

        [(action, ok, _, error)] = _activity(tmp_state_dir / "townloops.db")
        assert (action, ok) == ("run-failed", 0) and "signal 9" in error
        assert "signal 9" in telegrams[0]

    def test_successful_run_reports_nothing(self, wrapper, tmp_state_dir, telegrams):
        wrapper.fake_skill("print('ok')\n")
        log = tmp_state_dir / "logs" / "event-reconcile-test.log"
        log.parent.mkdir()

        assert wrapper.supervise(log, []) == 0
        assert _activity(tmp_state_dir / "townloops.db") == [] and telegrams == []


class TestSkillOverlap:
    def test_overlapping_run_is_skipped_not_failed(self, fresh_db, tmp_state_dir, monkeypatch, capsys):
        from engine import lock
        skill = _load(ROOT / "skills/business/event-reconcile/reconcile.py", "reconcile_overlap")
        db = tmp_state_dir / "townloops.db"
        monkeypatch.setattr("engine.inspection._DB_PATH", db)
        monkeypatch.setattr("engine.db.DEFAULT_DB_PATH", str(db))
        monkeypatch.setattr(skill, "open_db", lambda: fresh_db)
        monkeypatch.setattr(skill, "Live", lambda *a: pytest.fail("ran while locked"))

        with lock.acquire("event-reconcile"):
            assert skill.main(["run"]) == 0

        assert "previous run still active" in capsys.readouterr().out
        assert [(a, ok) for a, ok, _, _ in _activity(tmp_state_dir / "townloops.db")] == [
            ("skipped-overlap", 1)]
