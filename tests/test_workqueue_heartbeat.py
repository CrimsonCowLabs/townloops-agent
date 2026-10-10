"""scripts/workqueue_heartbeat.py — the 10-min cron wrapper detaches the drain
(like event_find.py), so a drain can run past Hermes' 120s cron script
budget; the `workqueue-drain` flock still keeps two drains from overlapping."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

from engine import lock, orchestrator

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def heartbeat(tmp_state_dir):
    spec = importlib.util.spec_from_file_location(
        "heartbeat_under_test", ROOT / "scripts" / "workqueue_heartbeat.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def popen(monkeypatch):
    launched: list[dict] = []

    class FakePopen:
        pid = 4242

        def __init__(self, cmd, **kw):
            launched.append({"cmd": cmd, **kw})

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    return launched


def test_cron_run_detaches_the_drain_and_returns_at_once(heartbeat, popen, tmp_state_dir,
                                                         monkeypatch):
    monkeypatch.setattr(orchestrator, "drain_workqueue",
                        lambda *a, **k: pytest.fail("drained inside the cron budget"))

    assert heartbeat.main([]) == 0

    [run] = popen
    assert run["cmd"][1:] == [str(heartbeat.SKILL), "--drain"]
    assert run["start_new_session"] is True
    assert Path(run["stdout"].name).parent == tmp_state_dir / "logs"
    assert Path(run["stdout"].name).name.startswith("workqueue-heartbeat-")


def test_the_detached_run_drains_in_process(heartbeat, popen, monkeypatch):
    drained = []
    monkeypatch.setattr(orchestrator, "drain_workqueue",
                        lambda *a, **k: drained.append(1) or {"per_action": {}})

    assert heartbeat.main(["--drain"]) == 0
    assert drained == [1] and popen == []


def test_a_drain_still_running_means_no_second_one(heartbeat, popen):
    with lock.acquire("workqueue-drain"):
        assert heartbeat.main([]) == 0
    assert popen == []
