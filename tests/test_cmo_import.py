"""One-off import of the CMO's stranded research drafts and business
candidates (engine/cmo_import.py).

Everything lands locally for the operator, nothing ships: candidates (and
business drafts) as `verified` business-discovery candidates the operator
can Promote, performer/creator/community drafts as `needs-review` entities
in the review queue. Each record is checked against live first; re-running
the import creates nothing new. TownLoops is the in-memory FakeAPI."""

from __future__ import annotations

import json
import sqlite3

from engine import business_discovery as bd
from engine import business_promote as bp
from engine import cmo_import
from engine import performer_research as pr
from engine.derive_helpers import insert_entity
from tests.fake_townloops import FakeAPI


def _cand(url, name, status="verified", town="slt", signal="web-search"):
    return {"town_slug": town, "canonical_url": url, "display_name": name, "status": status,
            "reject_reason": None if status == "verified" else "closed", "source_signal": signal,
            "first_seen_at": "2026-07-08T17:44:04-07:00", "last_seen_at": "2026-07-10T15:32:13-07:00",
            "verify_json": json.dumps({"verified": True, "reasons": ["in-town"],
                                       "phone": "530-555-0100"}),
            "research_url": None}


def _draft(url, kind, payload, image_url=None, note="from the official lineup"):
    return {"url": url, "kind": kind, "payload_json": json.dumps(payload), "image_url": image_url,
            "source_note": note, "fetched_at": "2026-07-23T14:38:43-07:00",
            "pushed_at": None, "townloops_slug": None}


WOLF = _draft("https://www.wolfjett.com/", "performer",
              {"name": "Wolf Jett", "kind": "band", "website": "https://www.wolfjett.com/",
               "instagram": "wolfjettmusic",
               "spotify_url": "https://open.spotify.com/artist/6A2D1C5Cl1jGNWncNQEmVB",
               "slug": "wolf-jett"})
LAKEVIEW = _draft("https://liveatlakeview.com/", "community",
                  {"name": "Live at Lakeview", "description": "Free Thursday concerts.",
                   "website": "https://liveatlakeview.com/",
                   "image_path": "https://liveatlakeview.com/Logo/logo-lakeview.jpeg",
                   "image_alt": "Live at Lakeview logo", "kind": "group",
                   "meeting_info": "Every Thursday 4:30-8:30 pm at Lakeview Commons",
                   "category_slug": "live-music", "slug": "live-at-lakeview"},
                  image_url="https://liveatlakeview.com/Logo/logo-lakeview.jpeg")
ONCOURSE = _draft("https://oncoursetahoe.com/", "business",
                  {"name": "On Course Events", "description": "An event production company.",
                   "website": "https://oncoursetahoe.com/", "category_slug": "arts",
                   "slug": "on-course-events"})


def _run(conn, api=None, drafts=(), candidates=()):
    return cmo_import.run_import(conn, api or FakeAPI(), "slt",
                                 drafts=list(drafts), candidates=list(candidates))


def _names(items):
    return sorted(i["name"] for i in items)


def _row(conn, table, name):
    r = conn.execute(f"SELECT * FROM {table} WHERE name = ?", (name,)).fetchone()
    return dict(r) if r else None


# --------------------------------------------------------------------------
# Business candidates
# --------------------------------------------------------------------------

def test_verified_candidates_are_proposed_and_live_ones_reported(fresh_db):
    api = FakeAPI(entities={"businesses": [
        {"slug": "tahoe-art-league", "name": "Tahoe Art League", "website": "https://talart.org/"}]})
    report = _run(fresh_db, api, candidates=[
        _cand("https://talart.org/", "Tahoe Art League"),
        _cand("https://www.alibialeworks.com/", "Alibi Ale Works"),
        _cand("https://closed.example/", "Closed Cafe", status="rejected"),
        _cand("https://elsewhere.example/", "Elsewhere Diner", town="tahoe-city"),
    ])

    c = report["candidates"]
    assert _names(c["imported"]) == ["Alibi Ale Works"]
    assert [(i["name"], i["live_slug"]) for i in c["already_live"]] == \
        [("Tahoe Art League", "tahoe-art-league")]
    assert _names(c["not_imported"]) == ["Closed Cafe", "Elsewhere Diner"]
    assert report["summary"]["candidates"] == {"imported": 1, "skipped": 0, "already_live": 1,
                                               "not_imported": 2}

    proposed = bd.list_candidates(fresh_db, "slt")
    assert [(r["display_name"], r["status"]) for r in proposed] == [("Alibi Ale Works", "verified")]
    assert proposed[0]["source_signal"] == "cmo-import:web-search"
    assert proposed[0]["verify"]["phone"] == "530-555-0100"
    live = bd.list_candidates(fresh_db, "slt", status="already-live")
    assert [r["live_slug"] for r in live] == ["tahoe-art-league"]


def test_rerunning_the_import_creates_nothing_new(fresh_db):
    cands = [_cand("https://www.alibialeworks.com/", "Alibi Ale Works")]
    drafts = [WOLF, LAKEVIEW, ONCOURSE]
    _run(fresh_db, drafts=drafts, candidates=cands)
    counts = {t: fresh_db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("discovery_candidates", "performers", "communities", "businesses")}

    again = _run(fresh_db, drafts=drafts, candidates=cands)

    assert counts == {t: fresh_db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in counts}
    assert again["summary"]["candidates"]["skipped"] == 1
    assert again["summary"]["drafts"] == {"imported": 0, "skipped": 3, "already_live": 0,
                                          "not_imported": 0}


def test_candidate_the_operator_declined_is_not_proposed_again(fresh_db):
    _run(fresh_db, candidates=[_cand("https://www.alibialeworks.com/", "Alibi Ale Works")])
    uid = bd.list_candidates(fresh_db, "slt")[0]["candidate_uid"]
    bp.decline(fresh_db, [uid])

    report = _run(fresh_db, candidates=[_cand("https://www.facebook.com/alibi", "Alibi Ale Works")])

    assert _names(report["candidates"]["not_imported"]) == ["Alibi Ale Works"]
    assert "declined" in report["candidates"]["not_imported"][0]["why"]
    assert bd.list_candidates(fresh_db, "slt") == []


# --------------------------------------------------------------------------
# Research drafts
# --------------------------------------------------------------------------

def test_business_draft_becomes_a_candidate_that_ships_its_draft_on_promote(fresh_db):
    api = FakeAPI()
    report = _run(fresh_db, api, drafts=[ONCOURSE])
    assert _names(report["drafts"]["imported"]) == ["On Course Events"]
    assert fresh_db.execute("SELECT COUNT(*) FROM businesses").fetchone()[0] == 0
    assert not [c for c in api.calls if c[0] not in ("GET", "LIST")]

    (cand,) = bd.list_candidates(fresh_db, "slt")
    assert (cand["status"], cand["source_signal"]) == ("verified", "cmo-import:research-draft")
    assert cand["verify"]["draft"]["description"] == "An event production company."
    bp.approve(fresh_db, [cand["candidate_uid"]])
    out = bp.promote_approved(fresh_db, api, "slt", enrich=lambda conn, uid: "enriched")

    assert out["promoted"] == 1
    biz = _row(fresh_db, "businesses", "On Course Events")
    assert biz["description"] == "An event production company."
    assert biz["category_slug"] == "arts"
    assert biz["website"] == "https://oncoursetahoe.com/"
    shipped = api.tables["businesses"][out["shipped"][0]["slug"]]
    assert shipped["description"] == "An event production company."


def test_performer_draft_waits_in_the_review_queue(fresh_db):
    api = FakeAPI()
    report = _run(fresh_db, api, drafts=[WOLF])

    assert _names(report["drafts"]["imported"]) == ["Wolf Jett"]
    row = _row(fresh_db, "performers", "Wolf Jett")
    assert row["enrichment_status"] == "needs-review"
    assert row["website"] is None and row["pushed_at"] is None   # nothing filled until accepted
    assert not [c for c in api.calls if c[0] not in ("GET", "LIST")]

    (item,) = pr.review_queue(fresh_db)
    assert (item["kind"], item["name"], item["status"]) == ("performer", "Wolf Jett", "imported")
    assert "official lineup" in item["why"]
    assert [c["url"] for c in item["candidates"]] == ["https://www.wolfjett.com/"]

    filled = pr.resolve(fresh_db, "performer", row["entity_uid"],
                        accept_url="https://www.wolfjett.com/")
    row = _row(fresh_db, "performers", "Wolf Jett")
    assert set(filled) >= {"website", "instagram", "spotify_url", "kind"}
    assert row["instagram"] == "https://www.instagram.com/wolfjettmusic/"
    assert row["kind"] == "band"
    assert row["enrichment_status"] == "enriched"


def test_community_draft_is_reviewable_and_drops_its_logo(fresh_db):
    _run(fresh_db, drafts=[LAKEVIEW])
    row = _row(fresh_db, "communities", "Live at Lakeview")
    assert row["enrichment_status"] == "needs-review"

    (item,) = pr.review_queue(fresh_db)
    assert item["kind"] == "community"
    fields = item["candidates"][0]["fields"]
    assert "image_path" not in fields and "image_alt" not in fields

    pr.resolve(fresh_db, "community", row["entity_uid"], accept_url="https://liveatlakeview.com/")
    row = _row(fresh_db, "communities", "Live at Lakeview")
    assert row["meeting_info"].startswith("Every Thursday")
    assert (row["kind"], row["category_slug"]) == ("group", "live-music")
    assert row["image_path"] is None


def test_draft_already_live_is_skipped_and_reported(fresh_db):
    api = FakeAPI(entities={
        "performers": [{"slug": "wolf-jett-band", "name": "Wolf Jett Band",
                        "website": "https://wolfjett.com"}],
        "communities": [{"slug": "lakeview-concerts", "name": "Live at Lakeview",
                         "website": None}]})
    report = _run(fresh_db, api, drafts=[WOLF, LAKEVIEW])

    assert [(i["name"], i["live_slug"]) for i in report["drafts"]["already_live"]] == \
        [("Wolf Jett", "wolf-jett-band"), ("Live at Lakeview", "lakeview-concerts")]
    assert _row(fresh_db, "performers", "Wolf Jett") is None


def test_performer_draft_is_never_matched_to_live_by_name_alone(fresh_db):
    api = FakeAPI(entities={"performers": [{"slug": "wolf-jett", "name": "Wolf Jett",
                                            "website": "https://someone-else.example/"}]})
    report = _run(fresh_db, api, drafts=[WOLF])

    assert _names(report["drafts"]["imported"]) == ["Wolf Jett"]


def test_draft_matching_a_local_entity_is_not_duplicated(fresh_db):
    insert_entity(fresh_db, "performers", "p-local", {
        "source": "event-derive-people", "source_kind": "mined", "name": "Wolf Jett"})
    report = _run(fresh_db, drafts=[WOLF])

    assert report["drafts"]["not_imported"][0]["entity_uid"] == "p-local"
    assert fresh_db.execute("SELECT COUNT(*) FROM performers").fetchone()[0] == 1


def test_loaders_read_unshipped_drafts_and_all_candidates(tmp_path):
    drafts_db = tmp_path / "drafts.db"
    c = sqlite3.connect(drafts_db)
    c.execute("CREATE TABLE drafts (url TEXT PRIMARY KEY, kind TEXT, payload_json TEXT, "
              "image_url TEXT, suggested_kind TEXT, fetched_at TEXT, saved_at TEXT, "
              "content_hash TEXT, pushed_at TEXT, pushed_hash TEXT, townloops_slug TEXT, "
              "source_note TEXT)")
    c.execute("INSERT INTO drafts (url, kind, payload_json, pushed_at) VALUES "
              "('https://a/', 'performer', '{}', NULL), ('https://b/', 'performer', '{}', '2026-07-01')")
    c.commit()
    cands_db = tmp_path / "candidates.db"
    c = sqlite3.connect(cands_db)
    c.execute("CREATE TABLE candidates (town_slug TEXT, canonical_url TEXT, status TEXT, "
              "first_seen_at TEXT)")
    c.execute("INSERT INTO candidates VALUES ('slt', 'https://x/', 'verified', '2026-07-08'), "
              "('slt', 'https://y/', 'rejected', '2026-07-08')")
    c.commit()

    assert [d["url"] for d in cmo_import.load_drafts(drafts_db)] == ["https://a/"]
    assert len(cmo_import.load_candidates(cands_db)) == 2
