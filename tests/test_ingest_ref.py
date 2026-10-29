"""engine/ingest_ref — the agent's identity key on TownLoops (#63)."""

import json
import re

from engine import ingest_ref
from engine.townloops_api import TownloopsError


class FakeClient:
    def __init__(self, rows=(), patch_error=None):
        self.rows = list(rows)
        self.patch_error = patch_error
        self.patches = []
        self.lists = []

    def list(self, resource, **params):
        self.lists.append((resource, params))
        return [r for r in self.rows if r.get("ingest_ref") == params.get("ingest_ref")]

    def update(self, resource, slug, payload):
        self.patches.append((resource, slug, payload))
        if self.patch_error:
            raise self.patch_error
        return {"slug": slug, **payload}


def test_refs_are_town_scoped_and_kept_apart(monkeypatch):
    monkeypatch.setenv("TOWNLOOPS_TOWN_SLUG", "slt")
    uid = "9a2008bc5fffa495deadbeef"
    assert ingest_ref.ref_for(uid) == "hermes-slt-9a2008bc5fffa495"
    assert len({ingest_ref.ref_for(uid), ingest_ref.change_ref_for(uid, {"start_at": "x"}),
                ingest_ref.cancel_ref_for(uid)}) == 3


def test_a_change_ref_is_per_change(monkeypatch):
    """A second, different change to one event is a new row with a new ref;
    the identical change retried keeps its ref (and so 409s and binds)."""
    monkeypatch.setenv("TOWNLOOPS_TOWN_SLUG", "slt")
    uid = "9a2008bc5fffa495deadbeef"
    a = ingest_ref.change_ref_for(uid, {"start_at": "2026-11-01T03:00:00+00:00"})
    assert a == ingest_ref.change_ref_for(uid, {"start_at": "2026-11-01T03:00:00+00:00"})
    assert a != ingest_ref.change_ref_for(uid, {"start_at": "2026-11-02T03:00:00+00:00"})
    assert a == ingest_ref.change_ref_for(uid, {"start_at": "2026-11-01T03:00:00+00:00"}.copy())
    assert re.fullmatch(r"hermes-slt-change-9a2008bc5fffa495-[0-9a-f]{8}", a)
    monkeypatch.setenv("TOWNLOOPS_TOWN_SLUG", "t" * 120)
    assert len(ingest_ref.change_ref_for(uid, {"venue_id": 7})) <= 160


def test_one_kind_to_resource_map():
    from engine import entity_sync, ref_adoption
    assert ingest_ref.RESOURCES == {"event": "events", **entity_sync.RESOURCES}
    assert ref_adoption.RESOURCES is ingest_ref.RESOURCES


def test_own_ref_is_the_stored_one_else_ours_and_none_for_a_mirror():
    row = {"entity_uid": "v" * 24, "source_kind": "derived", "ingest_ref": None}
    assert ingest_ref.own_ref("venue", row) == ingest_ref.ref_for("v" * 24)
    assert ingest_ref.own_ref("venue", {**row, "ingest_ref": "hermes-x-1"}) == "hermes-x-1"
    assert ingest_ref.own_ref("venue", {**row, "source_kind": "live"}) is None
    assert ingest_ref.own_ref("event", {"event_uid": "e" * 24, "source_kind": "scraped",
                                        "ingest_ref": None}) == ingest_ref.ref_for("e" * 24)


def test_taken_slug_reads_only_an_ingest_ref_conflict():
    taken = TownloopsError(409, "ingest_ref_taken", "x",
                           {"ingest_ref": "hermes-slt-1", "slug": "jazz-2026-08-01"})
    assert ingest_ref.taken_slug(taken) == "jazz-2026-08-01"
    assert ingest_ref.taken_slug(TownloopsError(409, "slug_taken", "x", {"slug": "a"})) is None
    assert ingest_ref.taken_slug(TownloopsError(409, "ingest_ref_taken", "x", {})) is None
    assert ingest_ref.taken_slug(RuntimeError("409")) is None


def test_find_by_ref_is_one_filtered_list_read():
    c = FakeClient([{"slug": "a", "ingest_ref": "r1"}, {"slug": "b", "ingest_ref": None}])
    assert ingest_ref.find_by_ref(c, "events", "r1")["slug"] == "a"
    assert ingest_ref.find_by_ref(c, "events", "r2") is None
    assert ingest_ref.find_by_ref(c, "events", None) is None
    assert [p["ingest_ref"] for _, p in c.lists] == ["r1", "r2"]


def test_adopt_sets_a_missing_ref_once():
    c = FakeClient()
    assert ingest_ref.adopt(c, "venues", {"slug": "the-mill"}, "r").status == "adopted"
    assert c.patches == [("venues", "the-mill", {"ingest_ref": "r"})]
    assert ingest_ref.adopt(c, "venues", {"slug": "m", "ingest_ref": "r"}, "r").status == "already"
    assert ingest_ref.adopt(c, "venues", {"slug": "m", "ingest_ref": "z"}, "r").status == "other-ref"
    assert len(c.patches) == 1


def test_adopt_reports_conflicts_without_raising():
    taken = TownloopsError(409, "ingest_ref_taken", "x", {"slug": "dup"})
    assert ingest_ref.adopt(FakeClient(patch_error=taken), "events", {"slug": "a"}, "r").status == "taken"
    refused = TownloopsError(422, "validation_error", "x", {"field": "ingest_ref"})
    assert ingest_ref.adopt(FakeClient(patch_error=refused), "events", {"slug": "a"}, "r").status == "other-ref"
    assert ingest_ref.adopt(FakeClient(patch_error=RuntimeError("boom")), "events",
                            {"slug": "a"}, "r").status == "error"


def test_remember_records_the_ref_live_carries(fresh_db):
    fresh_db.execute(
        "INSERT INTO venues (entity_uid, source, source_kind, name, first_seen_at, last_seen_at,"
        " content_hash, last_modified_at) VALUES ('v1', 's', 'mined', 'The Mill', 't', 't', 'h', 't')")
    ingest_ref.remember(fresh_db, "venue", "v1", "hermes-slt-v1")
    assert fresh_db.execute("SELECT ingest_ref FROM venues").fetchone()[0] == "hermes-slt-v1"
    for table in ingest_ref.RESOURCES.values():
        cols = {r[1] for r in fresh_db.execute(f"PRAGMA table_info({table})")}
        assert "ingest_ref" in cols, table


def test_adopt_onto_the_survivor_our_merged_row_resolves_to_is_already():
    """A row of ours merged into `riva` still holds the ref; TownLoops maps it
    to the survivor, so the ref already resolves there."""
    taken = TownloopsError(409, "ingest_ref_taken", "x", {"ingest_ref": "r", "slug": "riva"})
    c = FakeClient(patch_error=taken)
    assert ingest_ref.adopt(c, "venues", {"slug": "riva"}, "r").status == "already"
    assert ingest_ref.adopt(c, "venues", {"slug": "other"}, "r").status == "taken"


# --- adopt_and_remember: the one adopt → remember → record sequence -----------

def _mill(db, extra=None):
    db.execute(
        "INSERT INTO venues (entity_uid, source, source_kind, name, first_seen_at, last_seen_at,"
        " content_hash, last_modified_at, townloops_slug, extra_json)"
        " VALUES ('v1', 's', 'mined', 'The Mill', 't', 't', 'h', 't', 'the-mill', ?)",
        (json.dumps(extra) if extra else None,))


def _venue_row(db):
    return dict(db.execute("SELECT * FROM venues WHERE entity_uid = 'v1'").fetchone())


def _acts(db):
    return [dict(zip(("worker", "action", "target", "detail", "ok", "error"), r)) for r in db.execute(
        "SELECT worker, action, target, detail, ok, error FROM agent_activity ORDER BY id")]


def test_adopt_and_remember_adopts_remembers_and_records_once(fresh_db):
    _mill(fresh_db, {"ref_adoption": {"status": "error", "at": "t", "detail": "busy"}})
    c = FakeClient()
    result = ingest_ref.adopt_and_remember(c, fresh_db, "venue", "v1", {"slug": "the-mill"},
                                           worker="w")
    assert result.status == "adopted"
    row = _venue_row(fresh_db)
    assert row["ingest_ref"] == ingest_ref.ref_for("v1")
    assert "ref_adoption" not in json.loads(row["extra_json"] or "{}")   # stamp cleared
    [act] = _acts(fresh_db)
    assert (act["worker"], act["action"], act["target"], act["ok"]) == ("w", "adopt-ref", "venue:v1", 1)
    assert "adopted" in act["detail"] and "the-mill" in act["detail"]


def test_adopt_and_remember_already_carried_records_nothing(fresh_db):
    _mill(fresh_db)
    ref = ingest_ref.ref_for("v1")
    result = ingest_ref.adopt_and_remember(FakeClient(), fresh_db, "venue", "v1",
                                           {"slug": "the-mill", "ingest_ref": ref}, worker="w")
    assert result.status == "already"
    assert _venue_row(fresh_db)["ingest_ref"] == ref and _acts(fresh_db) == []


def test_a_final_outcome_is_marked_and_recorded(fresh_db):
    _mill(fresh_db)
    result = ingest_ref.adopt_and_remember(FakeClient(), fresh_db, "venue", "v1",
                                           {"slug": "the-mill", "ingest_ref": "cmo-1"}, worker="w")
    assert result.status == "other-ref"
    row = _venue_row(fresh_db)
    assert row["ingest_ref"] is None
    assert ingest_ref.final(row) == "other-ref"
    [act] = _acts(fresh_db)
    assert act["action"] == "adopt-ref" and "other-ref" in act["detail"]


def test_a_taken_ref_is_marked_final_and_a_failure(fresh_db):
    _mill(fresh_db)
    taken = TownloopsError(409, "ingest_ref_taken", "x", {"slug": "the-mill-2"})
    result = ingest_ref.adopt_and_remember(FakeClient(patch_error=taken), fresh_db, "venue", "v1",
                                           {"slug": "the-mill"}, worker="w")
    assert result.status == "taken"
    assert ingest_ref.final(_venue_row(fresh_db)) == "taken"
    [act] = _acts(fresh_db)
    assert act["action"] == "adopt-ref" and act["ok"] == 0 and "the-mill-2" in act["error"]
    assert "taken" in act["detail"]


def test_a_transient_error_is_stamped_not_final(fresh_db):
    _mill(fresh_db)
    result = ingest_ref.adopt_and_remember(FakeClient(patch_error=RuntimeError("busy")), fresh_db,
                                           "venue", "v1", {"slug": "the-mill"}, worker="w")
    assert result.status == "error"
    row = _venue_row(fresh_db)
    assert ingest_ref.final(row) is None
    assert json.loads(row["extra_json"])["ref_adoption"]["status"] == "error"
