"""Adapter fixture harness (#30): every case under tests/fixtures/adapters/<name>/
— a captured source response, the events it must read as and the seed it is
read for — runs through its registered adapter with fetching stubbed. A town
adapter promoted from `$HERMES_HOME/skill-proposals/engine-adapters/<name>/`
brings its case here."""

from __future__ import annotations

from pathlib import Path

import pytest

from engine import seed_adapters

CASES = sorted(d for d in (Path(__file__).parent / "fixtures" / "adapters").iterdir()
               if d.is_dir())


@pytest.mark.parametrize("case", CASES, ids=[d.name for d in CASES])
def test_the_capture_reads_as_expected(case, monkeypatch):
    monkeypatch.setenv("AGENT_TZ", "America/Los_Angeles")
    run = seed_adapters.run_case(case.name, case)
    assert run.expected is not None, f"{case.name}: no expected.json"
    assert run.diff == "", run.diff
