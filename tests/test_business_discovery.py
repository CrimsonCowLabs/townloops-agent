"""business-discovery: live dedup, the candidate store, and the sweep/verify
drivers (engine/business_discovery.py). Firecrawl is stubbed at the
engine.discovery seam; TownLoops is the in-memory FakeAPI."""

from __future__ import annotations

import pytest

from engine import business_discovery as bd
from engine import discovery
from engine.discovery import Candidate
from tests.fake_townloops import FakeAPI


LIVE = [
    {"slug": "cuppa-tahoe", "name": "Cuppa Tahoe", "website": "https://www.cuppatahoe.com/"},
    {"slug": "alibi-ale-works", "name": "Alibi Ale Works", "website": None},
    {"slug": "fb-only", "name": "Lakeside Yoga", "website": "https://facebook.com/lakesideyoga"},
]


def _index(rows=LIVE):
    return bd.LiveIndex.build(rows)


def _cand(url, name=None, signal="web-search:coffee"):
    return Candidate(canonical_url=discovery.canonicalize_url(url), display_name=name,
                     source_signal=signal)


def _rows(conn):
    return {r["canonical_url"]: dict(r) for r in conn.execute(
        "SELECT * FROM discovery_candidates ORDER BY canonical_url")}


# ---------------------------------------------------------------- live dedup

def test_live_match_on_fuzzy_name():
    m = _index().match("https://alibiale.example/", "Alibi Ale Works!")
    assert m and m.slug == "alibi-ale-works"
    assert "name" in m.reason


def test_live_match_name_threshold_is_085():
    idx = _index([{"slug": "x", "name": "Tahoe Coffee Roasters", "website": None}])
    assert idx.match("https://a.example/", "Tahoe Coffee Roaster")          # ~0.97
    assert idx.match("https://a.example/", "Tahoe Tea House") is None       # well below


def test_live_match_on_website_host_even_with_deeper_path():
    m = _index().match(discovery.canonicalize_url("https://cuppatahoe.com/menu"), "Some Other Title")
    assert m and m.slug == "cuppa-tahoe"
    assert "website" in m.reason


def test_a_location_page_website_only_covers_pages_under_it():
    idx = _index([{"slug": "aleworx-stateline", "name": "AleWorX Stateline",
                   "website": "https://laketahoealeworx.com/stateline/"}])
    url = discovery.canonicalize_url
    assert idx.match(url("https://laketahoealeworx.com/stateline/menu"), None).slug == "aleworx-stateline"
    assert idx.match(url("https://laketahoealeworx.com/south-lake"), "AleWorX South Lake") is None
    assert idx.match(url("https://laketahoealeworx.com/stateline-north"), None) is None


def test_shared_platform_host_needs_the_exact_page():
    idx = _index()
    assert idx.match(discovery.canonicalize_url("https://facebook.com/someoneelse"), None) is None
    m = idx.match(discovery.canonicalize_url("https://www.facebook.com/lakesideyoga/"), None)
    assert m and m.slug == "fb-only"


def test_no_name_no_url_match_is_not_live():
    assert _index().match("https://brand-new.example", None) is None


# ---------------------------------------------------------------- store

def test_record_sweep_inserts_new_and_marks_already_live(fresh_db):
    counts = bd.record_sweep(fresh_db, "slt", [
        _cand("https://brand-new.example/", "Brand New Bakery"),
        _cand("https://cuppatahoe.com/about", "Cuppa"),
    ], _index())
    assert counts == {"seen": 2, "inserted": 1, "already_live": 1, "updated": 0}
    rows = _rows(fresh_db)
    assert rows["https://brand-new.example/"]["status"] == "new"
    live = rows["https://cuppatahoe.com/about"]
    assert live["status"] == "already-live"
    assert live["live_slug"] == "cuppa-tahoe"
    assert live["reject_reason"]


def test_record_sweep_is_idempotent(fresh_db):
    c = [_cand("https://brand-new.example/", "Brand New Bakery")]
    bd.record_sweep(fresh_db, "slt", c, _index())
    counts = bd.record_sweep(fresh_db, "slt", c, _index())
    assert counts["inserted"] == 0 and counts["updated"] == 1
    assert len(_rows(fresh_db)) == 1


def test_resweep_flips_a_pending_candidate_that_went_live(fresh_db):
    c = [_cand("https://brand-new.example/", "Brand New Bakery")]
    bd.record_sweep(fresh_db, "slt", c, _index())
    went_live = LIVE + [{"slug": "brand-new-bakery", "name": "Brand New Bakery", "website": None}]
    counts = bd.record_sweep(fresh_db, "slt", c, _index(went_live))
    assert counts["already_live"] == 1
    row = _rows(fresh_db)["https://brand-new.example/"]
    assert (row["status"], row["live_slug"]) == ("already-live", "brand-new-bakery")


def test_resweep_never_resurrects_a_rejected_candidate(fresh_db):
    c = [_cand("https://brand-new.example/", "Brand New Bakery")]
    bd.record_sweep(fresh_db, "slt", c, _index())
    fresh_db.execute("UPDATE discovery_candidates SET status = 'rejected', reject_reason = 'operator'")
    bd.record_sweep(fresh_db, "slt", c, _index())
    assert _rows(fresh_db)["https://brand-new.example/"]["status"] == "rejected"


def test_store_round_trip_through_verify_and_list(fresh_db):
    bd.record_sweep(fresh_db, "slt", [
        _cand("https://brand-new.example/", "Brand New Bakery"),
        _cand("https://closed.example/", "Closed Cafe"),
        _cand("https://cuppatahoe.com/", "Cuppa Tahoe"),
    ], _index())
    pending = bd.pending_verify(fresh_db, "slt", limit=10)
    assert [p["canonical_url"] for p in pending] == ["https://brand-new.example/", "https://closed.example/"]

    ok = {"verified": True, "reasons": ["in-town"], "address": "1 Main St", "title": "Brand New Bakery"}
    assert bd.record_verdict(fresh_db, pending[0], ok, _index()) == "verified"
    bad = {"verified": False, "reasons": ["closure marker found: 'permanently closed'"]}
    assert bd.record_verdict(fresh_db, pending[1], bad, _index()) == "rejected"

    proposed = bd.list_candidates(fresh_db, "slt")
    assert [c["display_name"] for c in proposed] == ["Brand New Bakery"]
    assert proposed[0]["status"] == "verified"
    assert proposed[0]["verify"]["address"] == "1 Main St"

    everything = bd.list_candidates(fresh_db, "slt", status="all")
    assert {c["status"] for c in everything} == {"verified", "rejected", "already-live"}
    rejected = bd.list_candidates(fresh_db, "slt", status="rejected")
    assert "closure marker" in rejected[0]["reject_reason"]

    assert bd.stats(fresh_db, "slt") == {"verified": 1, "rejected": 1, "already-live": 1}


def test_verdict_never_overwrites_an_operator_decision_made_mid_run(fresh_db):
    bd.record_sweep(fresh_db, "slt", [_cand("https://brand-new.example/", "Brand New Bakery")], _index())
    row = bd.pending_verify(fresh_db, "slt", limit=1)[0]
    fresh_db.execute("UPDATE discovery_candidates SET status = 'rejected', reject_reason = 'operator'")
    assert bd.record_verdict(fresh_db, row, {"verified": True, "reasons": []}, _index()) is None
    stored = _rows(fresh_db)["https://brand-new.example/"]
    assert (stored["status"], stored["reject_reason"]) == ("rejected", "operator")


def test_verify_names_a_directory_candidate_from_its_page_title_and_dedups(fresh_db):
    bd.record_sweep(fresh_db, "slt", [_cand("https://alibi.example/", None, "directory:x")], _index())
    row = bd.pending_verify(fresh_db, "slt", limit=1)[0]
    verdict = {"verified": True, "reasons": [], "title": "Home | Alibi Ale Works"}
    assert bd.record_verdict(fresh_db, row, verdict, _index()) == "already-live"
    stored = _rows(fresh_db)["https://alibi.example/"]
    assert stored["display_name"] == "Alibi Ale Works"
    assert stored["live_slug"] == "alibi-ale-works"


def test_list_is_scoped_to_the_town(fresh_db):
    bd.record_sweep(fresh_db, "other-town", [_cand("https://x.example/", "X")], _index())
    assert bd.list_candidates(fresh_db, "slt") == []


# ---------------------------------------------------------------- drivers

PROFILE = {
    "name": "South Lake Tahoe", "region": "California",
    "discovery": {"business": {
        "category_query_terms": ["coffee shops"],
        "seed_directories": ["https://visit.example/dining"],
    }},
}


def test_run_sweep_fans_out_sources_and_dedups_against_live(fresh_db, monkeypatch):
    seen = {}

    def web(town, region, templates, **kw):
        seen["templates"] = list(templates)
        return [_cand("https://cuppatahoe.com/", "Cuppa Tahoe"),
                _cand("https://brand-new.example/", "Brand New Bakery")]

    def dirs(seeds, **kw):
        seen["seeds"] = list(seeds)
        return [_cand("https://dir-find.example/", None, "directory:visit.example")]

    monkeypatch.setattr(discovery, "sweep_web_search", web)
    monkeypatch.setattr(discovery, "sweep_directories", dirs)
    monkeypatch.setattr(discovery, "sweep_facebook",
                        lambda *a, **k: pytest.fail("facebook not opted in"))
    api = FakeAPI(entities={"businesses": LIVE})

    out = bd.run_sweep(fresh_db, api, PROFILE, "slt", per_source=5)

    assert "coffee shops in South Lake Tahoe, California" in seen["templates"]
    assert seen["seeds"] == ["https://visit.example/dining"]
    assert out["inserted"] == 2 and out["already_live"] == 1
    assert ("LIST", "businesses") in [c[:2] for c in api.calls]


def test_run_verify_rechecks_live_and_records_verdicts(fresh_db, monkeypatch):
    bd.record_sweep(fresh_db, "slt", [
        _cand("https://brand-new.example/", "Brand New Bakery"),
        _cand("https://gone.example/", "Gone Diner"),
    ], _index())
    verdicts = {
        "https://brand-new.example/": {"verified": True, "reasons": [], "phone": "530-555-0100"},
        "https://gone.example/": {"verified": False, "reasons": ["no mention of town"]},
    }
    calls = []

    def verify(url, **kw):
        calls.append((url, kw))
        return verdicts[url]

    monkeypatch.setattr(discovery, "verify_candidate", verify)
    profile = {**PROFILE, "name_aliases": ["SLT"]}
    out = bd.run_verify(fresh_db, FakeAPI(entities={"businesses": LIVE}), profile, "slt", limit=10)

    assert out == {"processed": 2, "verified": 1, "rejected": 1, "already_live": 0,
                   "deferred": 0, "errors": []}
    assert calls[0][1] == {"town_name": "South Lake Tahoe", "region": "California", "aliases": ["SLT"]}


def test_run_verify_leaves_a_candidate_pending_when_the_verifier_crashes(fresh_db, monkeypatch):
    bd.record_sweep(fresh_db, "slt", [_cand("https://boom.example/", "Boom")], _index())

    def boom(url, **kw):
        raise RuntimeError("firecrawl down")

    monkeypatch.setattr(discovery, "verify_candidate", boom)
    out = bd.run_verify(fresh_db, FakeAPI(entities={"businesses": LIVE}), PROFILE, "slt", limit=10)
    assert out == {"processed": 1, "verified": 0, "rejected": 0, "already_live": 0,
                   "deferred": 1, "errors": ["https://boom.example/: RuntimeError: firecrawl down"]}
    assert _rows(fresh_db)["https://boom.example/"]["status"] == "new"
