"""Adapter fixture harness (#30): every case under tests/fixtures/adapters/<name>/
— a captured source response, the events it must read as and the seed it is
read for — runs through its adapter with fetching stubbed. A case that
carries `<name>.py` and names no shared adapter is a town adapter, read the
way a town reads it (from $HERMES_HOME/adapters/) — the shape a promotion
proposal arrives in (`skill-proposals/engine-adapters/<name>/`)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from engine import seed_adapters

CASES = sorted(d for d in (Path(__file__).parent / "fixtures" / "adapters").iterdir()
               if d.is_dir())


@pytest.mark.parametrize("case", CASES, ids=[d.name for d in CASES])
def test_the_capture_reads_as_expected(case, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_TZ", "America/Los_Angeles")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    town_file = case / f"{case.name}.py"
    if case.name not in seed_adapters.ADAPTERS:
        assert town_file.exists(), f"{case.name}: no shared adapter and no {town_file.name}"
        (tmp_path / "adapters").mkdir()
        shutil.copy(town_file, tmp_path / "adapters" / town_file.name)
    run = seed_adapters.run_case(case.name, case)
    assert run.expected is not None, f"{case.name}: no expected.json"
    assert run.diff == "", run.diff
