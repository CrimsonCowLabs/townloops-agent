"""The nightly digest's shipped listing collapses Series (issue #3): events
shipped in the last 24h appear one line per Series with a date count, one
line per singleton; the Telegram summary counts Series and one-offs apart.
Driven through `scripts/onboarding_summary.py`'s `main`."""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


@pytest.fixture
def digest(fresh_db, tmp_state_dir, monkeypatch, capsys):
    import engine.db as db_mod
    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", tmp_state_dir / "townloops.db")
    sent: list[str] = []
    monkeypatch.setattr("engine.orchestrator._telegram_notify", sent.append)
    path = Path(__file__).resolve().parent.parent / "scripts/onboarding_summary.py"
    spec = importlib.util.spec_from_file_location("onboarding_summary", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    def go():
        capsys.readouterr()
        assert mod.main() == 0
        out = json.loads(capsys.readouterr().out)
        return out, Path(out["report_path"]).read_text(), sent[-1]

    return go


def _shipped(conn, uid, title, date_utc, hours_ago=1, **kw):
    at = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat(timespec="seconds")
    cols = {"event_uid": uid, "source": "seed", "source_kind": "seed", "name": title, "title": title,
            "date": date_utc, "date_utc": date_utc, "date_local": date_utc, "date_day": date_utc[:10],
            "state": "shipped", "approval_status": "approved", "state_updated_at": at,
            "first_seen_at": at, "last_seen_at": at, "last_modified_at": at,
            "content_hash": uid, "townloops_slug": uid}
    cols.update(kw)
    conn.execute(f"INSERT INTO events ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                 tuple(cols.values()))
    conn.commit()


def test_shipped_listing_collapses_series(fresh_db, digest):
    for i in range(7):
        _shipped(fresh_db, f"music-{i}", "Daily Live Music", f"2026-10-{12 + i}T02:00:00Z",
                 series_slug="daily-live-music")
    _shipped(fresh_db, "jazz-riva", "Jazz Night", "2026-10-15T02:00:00Z")
    _shipped(fresh_db, "jazz-hub", "Jazz Night", "2026-10-15T03:00:00Z")
    _shipped(fresh_db, "old", "Old Thing", "2026-10-15T03:00:00Z", hours_ago=30)

    out, md, tg = digest()
    assert out["shipped"] == {"events": 9, "series": 1, "singletons": 2}
    section = md.split("## Shipped")[1]
    lines = [l for l in section.splitlines() if l.startswith("- ")]
    assert len(lines) == 3
    assert any("Daily Live Music" in l and "7 dates" in l and "daily-live-music" in l for l in lines)
    assert sum("Jazz Night" in l for l in lines) == 2
    assert "Old Thing" not in md
    assert "shipped: 9 events (1 Series, 2 one-off)" in tg
