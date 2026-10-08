"""entity-live-sync under a Hermes no_agent cron: Hermes passes a script no
arguments, so the wrapper carries the nightly limit itself, and it detaches
so a slow sweep can't be killed by the 120s script timeout mid-PATCH."""

from __future__ import annotations

import ast
import importlib.util
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(tmp_state_dir, tmp_path, monkeypatch, argv: list[str]) -> tuple[list[str], int]:
    """Run the wrapper against a fake skill; return the skill's argv and session id."""
    skill = tmp_path / "fake_sync.py"
    skill.write_text(
        "import os, sys, time\n"
        "time.sleep(1.5)\n"
        "print('args', repr(sys.argv[1:]), 'sid', os.getsid(0), flush=True)\n"
    )
    wrapper = _load(ROOT / "scripts/entity_live_sync.py", "entity_live_sync_wrapper")
    monkeypatch.setattr(wrapper, "SKILL", skill)
    monkeypatch.setattr(sys, "argv", ["entity_live_sync.py", *argv])
    monkeypatch.setenv("PYTHON", sys.executable)

    t0 = time.monotonic()
    assert wrapper.main() == 0
    assert time.monotonic() - t0 < 1.0

    logs = list((tmp_state_dir / "logs").glob("entity-live-sync-*.log"))
    assert len(logs) == 1
    deadline = time.monotonic() + 10
    while "args" not in logs[0].read_text() and time.monotonic() < deadline:
        time.sleep(0.1)
    out = logs[0].read_text()
    args = ast.literal_eval(out.split("args", 1)[1].split(" sid")[0])
    return args, int(out.split("sid")[1].split()[0])


def test_no_args_runs_the_nightly_limit_detached(tmp_state_dir, tmp_path, monkeypatch):
    args, child_sid = _run(tmp_state_dir, tmp_path, monkeypatch, [])
    assert args == ["run", "--limit", "15"]
    assert child_sid != os.getsid(0)   # own session: survives the cron script being killed


def test_explicit_args_replace_the_default(tmp_state_dir, tmp_path, monkeypatch):
    args, _ = _run(tmp_state_dir, tmp_path, monkeypatch, ["--limit", "2", "--kind", "venue"])
    assert args == ["run", "--limit", "2", "--kind", "venue"]


def test_missing_skill_fails_loudly(tmp_state_dir, tmp_path, monkeypatch, capsys):
    wrapper = _load(ROOT / "scripts/entity_live_sync.py", "entity_live_sync_wrapper_missing")
    monkeypatch.setattr(wrapper, "SKILL", tmp_path / "nope.py")
    assert wrapper.main() == 1
    assert "skill missing" in capsys.readouterr().err
