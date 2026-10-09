"""operator-list-events collapses Series (issue #3): the chat listing shows
one row per Series with its date count, a singleton keeps its own row (no
title merging), and `--series <slug>` expands a Series into every live
occurrence through `?series=`. Driven through the skill's `main` against a
fresh local DB and the fake API."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from tests.fake_townloops import FakeAPI


@pytest.fixture
def run(fresh_db, tmp_state_dir, monkeypatch, capsys):
    import engine.db as db_mod
    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", tmp_state_dir / "townloops.db")
    path = Path(__file__).resolve().parent.parent / "skills/business/operator-list-events/list.py"
    spec = importlib.util.spec_from_file_location("operator_list_events", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    def go(*argv, api=None):
        if api is not None:
            import engine.townloops_api as tl
            monkeypatch.setattr(tl, "Client", lambda: api)
        capsys.readouterr()
        assert mod.main(list(argv)) == 0
        return json.loads(capsys.readouterr().out)

    return go


def _local(conn, uid, date_utc, title, **kw):
    cols = {"event_uid": uid, "source": "seed", "source_kind": "seed", "name": title, "title": title,
            "date": date_utc, "date_utc": date_utc, "date_local": date_utc, "date_day": date_utc[:10],
            "state": "shipped", "approval_status": "approved",
            "first_seen_at": "2026-10-01T00:00:00Z", "last_seen_at": "2026-10-01T00:00:00Z",
            "last_modified_at": "2026-10-01T00:00:00Z", "state_updated_at": "2026-10-01T00:00:00Z",
            "content_hash": uid, "townloops_slug": uid}
    cols.update(kw)
    conn.execute(f"INSERT INTO events ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                 tuple(cols.values()))
    conn.commit()


def test_listing_collapses_series_to_one_row_with_date_count(fresh_db, run):
    for i in range(3):
        _local(fresh_db, f"trivia-{i}", f"2026-10-{15 + 7 * i}T02:00:00Z", "Trivia Thursday",
               series_slug="trivia-thursday", series_id=1)
    _local(fresh_db, "jazz-riva", "2026-10-16T02:00:00Z", "Jazz Night", venue_name="Riva Grill")
    _local(fresh_db, "jazz-hub", "2026-10-16T03:00:00Z", "Jazz Night", venue_name="Social House")

    out = run("--status", "approved")
    assert out["count"] == 3 and out["events_total"] == 5
    trivia = next(e for e in out["events"] if e["series_slug"] == "trivia-thursday")
    assert trivia["event_uid"] == "trivia-0" and trivia["dates"] == 3
    jazz = [e for e in out["events"] if e["title"] == "Jazz Night"]
    assert len(jazz) == 2 and all(e["dates"] == 1 and e["series_slug"] is None for e in jazz)


def test_limit_counts_groups(fresh_db, run):
    for i in range(5):
        _local(fresh_db, f"music-{i}", f"2026-10-{12 + i}T02:00:00Z", "Daily Live Music",
               series_slug="daily-live-music")
    _local(fresh_db, "book-talk", "2026-10-20T02:00:00Z", "Book Talk")
    out = run("--status", "approved", "--limit", "2")
    assert [e["dates"] for e in out["events"]] == [5, 1]


def test_series_expansion_lists_every_occurrence(run):
    series = [{"id": 1, "slug": "trivia-thursday", "title": "Trivia Thursday",
               "cadence_text": "Weekly on Thursdays", "url": "/slt/series/trivia-thursday"}]
    events = [{"slug": f"trivia-{i}", "title": "Trivia Thursday",
               "start_at": f"2026-{10 + i // 4}-{1 + 7 * (i % 4):02d}T02:00:00Z",
               "venue_slug": "riva-grill", "status": "published", "series_slug": "trivia-thursday"}
              for i in range(30)]
    events.append({"slug": "other", "title": "Other", "start_at": "2026-10-02T02:00:00Z",
                   "status": "published"})
    api = FakeAPI(events, series=series)
    out = run("--series", "trivia-thursday", api=api)
    assert out["series"]["slug"] == "trivia-thursday"
    assert out["series"]["cadence_text"] == "Weekly on Thursdays"
    assert out["count"] == 30 and len(out["events"]) == 30
    assert {e["slug"] for e in out["events"]} == {f"trivia-{i}" for i in range(30)}
    assert out["events"] == sorted(out["events"], key=lambda e: e["start_at"])
    assert {c[0] for c in api.calls} <= {"LIST", "GET"}
