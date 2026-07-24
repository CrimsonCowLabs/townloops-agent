"""Tests for engine/enrichment.py — claim → apply → ship state machine
plus the resync_from_remote overwrite-prevention (P2.4) and sweep lock
(P1.4).

`enrichment` couples: DB rows, `engine.research.research_url` (mocked),
`engine.townloops_api.Client` (mocked), and `engine.lock`. All external
dependencies are patched so tests never touch a network or a shared file.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from unittest.mock import MagicMock, patch

import pytest


# =====================================================================
# Fixtures
# =====================================================================


@dataclass
class _FakeResearchResult:
    """Duck-types engine.research.ResearchResult for the mocked research_url."""
    url: str = ""
    kind: str = "business"
    confidence: float = 0.9
    payload: dict = field(default_factory=dict)
    fetched_from: list = field(default_factory=list)
    notes: list = field(default_factory=list)


@pytest.fixture
def seed_business(fresh_db):
    """Insert a business row + its enrichment_queue entry. Returns entity_uid."""
    uid = "biz-uid-abc"
    fresh_db.execute(
        """
        INSERT INTO businesses(
            entity_uid, source, source_kind, name,
            first_seen_at, last_seen_at, content_hash, last_modified_at
        ) VALUES (?, 'test', 'mined', 'Test Cafe',
                  '2026-07-01', '2026-07-01', 'hash1', '2026-07-01')
        """,
        (uid,),
    )
    fresh_db.execute(
        "INSERT INTO enrichment_queue(kind, entity_uid, queued_at) "
        "VALUES ('business', ?, '2026-07-01')",
        (uid,),
    )
    fresh_db.commit()
    return uid


# =====================================================================
# claim()
# =====================================================================


class TestClaim:
    def test_marks_rows_claimed(self, fresh_db, seed_business):
        from engine import enrichment
        ids = enrichment.claim("business", limit=10, conn=fresh_db)
        assert len(ids) == 1
        row = fresh_db.execute(
            "SELECT claimed_at, claimed_by, attempts FROM enrichment_queue WHERE id = ?",
            (ids[0],),
        ).fetchone()
        assert row["claimed_at"] is not None
        assert row["claimed_by"] is not None
        assert row["attempts"] == 1

    def test_skips_already_claimed(self, fresh_db, seed_business):
        from engine import enrichment
        first = enrichment.claim("business", limit=10, conn=fresh_db)
        second = enrichment.claim("business", limit=10, conn=fresh_db)
        assert first  # got claimed rows on first call
        assert second == [], "second call should return no unclaimed IDs"

    def test_respects_limit(self, fresh_db):
        from engine import enrichment
        for i in range(5):
            uid = f"biz-{i}"
            fresh_db.execute(
                """
                INSERT INTO businesses(
                    entity_uid, source, source_kind, name,
                    first_seen_at, last_seen_at, content_hash, last_modified_at
                ) VALUES (?, 't', 'mined', ?, 'x', 'x', 'x', 'x')
                """,
                (uid, f"Cafe {i}"),
            )
            fresh_db.execute(
                "INSERT INTO enrichment_queue(kind, entity_uid, queued_at) "
                "VALUES ('business', ?, 'x')",
                (uid,),
            )
        fresh_db.commit()
        assert len(enrichment.claim("business", limit=3, conn=fresh_db)) == 3


# =====================================================================
# apply()
# =====================================================================


class TestApply:
    def test_fills_null_fields(self, fresh_db, seed_business, monkeypatch):
        from engine import enrichment
        # research_url is patched to return canned findings.
        result = _FakeResearchResult(payload={
            "description": "Great coffee.",
            "phone": "+15305551234",
            "website": "https://testcafe.example",
        })
        monkeypatch.setattr(enrichment._research, "research_url",
                              lambda url, use_firecrawl=False: result)

        # First manually give the business a website (so research_url has a URL to hit).
        fresh_db.execute(
            "UPDATE businesses SET website = 'https://seed.example' WHERE entity_uid = ?",
            (seed_business,),
        )
        fresh_db.commit()

        qids = enrichment.claim("business", limit=10, conn=fresh_db)
        summaries = enrichment.apply("business", qids, conn=fresh_db)

        row = fresh_db.execute(
            "SELECT description, phone, website, enrichment_status FROM businesses WHERE entity_uid = ?",
            (seed_business,),
        ).fetchone()
        # NULL fields got filled…
        assert row["description"] == "Great coffee."
        assert row["phone"] == "+15305551234"
        # …but pre-existing `website` was NOT overwritten (P1.1/P2.4 semantics).
        assert row["website"] == "https://seed.example"
        assert row["enrichment_status"] == "ready"
        assert summaries[0]["filled"] == ["description", "phone"]

    def test_youtube_alias_mirrored(self, fresh_db, seed_business, monkeypatch):
        """P1.1: `youtube` on payload should also populate for creators
        that store as `youtube_url`, and vice versa."""
        from engine import enrichment
        result = _FakeResearchResult(payload={"youtube_url": "https://youtu.be/x"})
        monkeypatch.setattr(enrichment._research, "research_url",
                              lambda url, use_firecrawl=False: result)
        fresh_db.execute("UPDATE businesses SET website = 'https://x' WHERE entity_uid = ?",
                          (seed_business,))
        fresh_db.commit()
        qids = enrichment.claim("business", limit=10, conn=fresh_db)
        enrichment.apply("business", qids, conn=fresh_db)
        # Business fillable includes `youtube` (bare), and the mirror should
        # have populated it from `youtube_url`.
        row = fresh_db.execute(
            "SELECT youtube FROM businesses WHERE entity_uid = ?", (seed_business,)
        ).fetchone()
        assert row["youtube"] == "https://youtu.be/x"

    def test_row_with_no_url_is_skipped(self, fresh_db, seed_business, monkeypatch):
        from engine import enrichment
        # Website + source_url both NULL → nothing to research.
        research_mock = MagicMock()
        monkeypatch.setattr(enrichment._research, "research_url", research_mock)
        qids = enrichment.claim("business", limit=10, conn=fresh_db)
        summaries = enrichment.apply("business", qids, conn=fresh_db)
        research_mock.assert_not_called()
        assert summaries[0]["skipped"] is True
        # The queue row should be resolved with a helpful last_error.
        row = fresh_db.execute(
            "SELECT resolved_at, last_error FROM enrichment_queue WHERE id = ?", (qids[0],)
        ).fetchone()
        assert row["resolved_at"] is not None
        assert "no url" in row["last_error"]

    def test_all_fields_already_filled_marked_done(self, fresh_db, seed_business, monkeypatch):
        """Nothing to fill → enrichment_status = 'done' (not 'ready')."""
        from engine import enrichment
        fresh_db.execute(
            """UPDATE businesses SET
                website = 'https://x',
                description = 'already set',
                phone = 'already set',
                address = 'already set',
                image_path = 'already set',
                image_alt = 'already set',
                instagram = 'already set',
                facebook = 'already set',
                tiktok = 'already set',
                twitter = 'already set',
                youtube = 'already set',
                linkedin = 'already set'
                WHERE entity_uid = ?""",
            (seed_business,),
        )
        fresh_db.commit()
        monkeypatch.setattr(
            enrichment._research, "research_url",
            lambda url, use_firecrawl=False: _FakeResearchResult(
                payload={"description": "would overwrite but shouldn't"}
            ),
        )
        qids = enrichment.claim("business", limit=10, conn=fresh_db)
        enrichment.apply("business", qids, conn=fresh_db)
        row = fresh_db.execute(
            "SELECT description, enrichment_status FROM businesses WHERE entity_uid = ?",
            (seed_business,),
        ).fetchone()
        assert row["description"] == "already set"
        assert row["enrichment_status"] == "done"


# =====================================================================
# ship()
# =====================================================================


class TestShip:
    def test_ships_ready_rows_and_marks_done(self, fresh_db, seed_business, monkeypatch):
        from engine import enrichment
        fresh_db.execute(
            """UPDATE businesses SET
                website = 'https://x',
                description = 'to ship',
                enrichment_status = 'ready',
                townloops_slug = 'test-cafe'
                WHERE entity_uid = ?""",
            (seed_business,),
        )
        fresh_db.commit()

        # Patch the Client so no real HTTP happens.
        mock_client = MagicMock()
        monkeypatch.setattr(enrichment.townloops_api, "Client", lambda: mock_client)

        result = enrichment.ship("business", conn=fresh_db)
        assert result["shipped"] == 1
        assert result["errors"] == []
        mock_client.update.assert_called_once()
        args, _ = mock_client.update.call_args
        assert args[0] == "businesses"
        assert args[1] == "test-cafe"
        assert args[2] == {"website": "https://x", "description": "to ship"}
        # Row is now 'done'.
        row = fresh_db.execute(
            "SELECT enrichment_status, pushed_at FROM businesses WHERE entity_uid = ?",
            (seed_business,),
        ).fetchone()
        assert row["enrichment_status"] == "done"
        assert row["pushed_at"] is not None

    def test_skips_rows_without_townloops_slug(self, fresh_db, seed_business, monkeypatch):
        """No slug → can't PATCH → row deliberately skipped."""
        from engine import enrichment
        fresh_db.execute(
            "UPDATE businesses SET description = 'x', enrichment_status = 'ready' "
            "WHERE entity_uid = ?",
            (seed_business,),
        )
        fresh_db.commit()
        mock_client = MagicMock()
        monkeypatch.setattr(enrichment.townloops_api, "Client", lambda: mock_client)
        result = enrichment.ship("business", conn=fresh_db)
        assert result["shipped"] == 0
        mock_client.update.assert_not_called()

    def test_townloops_error_recorded_no_abort(self, fresh_db, monkeypatch):
        """One row failing to PATCH must not stop the batch."""
        from engine import enrichment
        from engine.townloops_api import TownloopsError

        for i in range(2):
            uid = f"biz-{i}"
            fresh_db.execute(
                """INSERT INTO businesses(
                    entity_uid, source, source_kind, name, description,
                    first_seen_at, last_seen_at, content_hash, last_modified_at,
                    enrichment_status, townloops_slug
                ) VALUES (?, 't', 'mined', ?, 'to ship', 'x', 'x', 'x', 'x', 'ready', ?)""",
                (uid, f"Cafe {i}", f"slug-{i}"),
            )
        fresh_db.commit()

        mock_client = MagicMock()
        mock_client.update.side_effect = [
            TownloopsError(422, "validation_error", "bad"),
            {"ok": True},
        ]
        monkeypatch.setattr(enrichment.townloops_api, "Client", lambda: mock_client)

        result = enrichment.ship("business", conn=fresh_db)
        assert result["shipped"] == 1
        assert len(result["errors"]) == 1
        assert result["errors"][0]["code"] == "validation_error"


# =====================================================================
# resync_from_remote() — P2.4
# =====================================================================


class TestResyncFromRemote:
    def test_copies_remote_non_null_fields_to_local(self, fresh_db, seed_business, monkeypatch):
        from engine import enrichment
        fresh_db.execute(
            "UPDATE businesses SET townloops_slug = 'test-cafe' WHERE entity_uid = ?",
            (seed_business,),
        )
        fresh_db.commit()

        mock_client = MagicMock()
        mock_client.get.return_value = {
            "name": "Test Cafe",
            "description": "Fetched from remote",
            "phone": "+15305559999",
            "instagram": None,  # NULL on remote → do NOT copy
        }
        monkeypatch.setattr(enrichment.townloops_api, "Client", lambda: mock_client)

        result = enrichment.resync_from_remote("business", conn=fresh_db)
        assert result["updated"] == 1

        row = fresh_db.execute(
            "SELECT description, phone, instagram FROM businesses WHERE entity_uid = ?",
            (seed_business,),
        ).fetchone()
        assert row["description"] == "Fetched from remote"
        assert row["phone"] == "+15305559999"
        assert row["instagram"] is None  # NULL remote → left NULL

    def test_never_touches_bookkeeping_columns(self, fresh_db, seed_business, monkeypatch):
        """Protected columns (entity_uid, content_hash, source, ...) must not
        be overwritten even if remote returns values for them."""
        from engine import enrichment
        fresh_db.execute(
            "UPDATE businesses SET townloops_slug = 'test-cafe' WHERE entity_uid = ?",
            (seed_business,),
        )
        fresh_db.commit()

        mock_client = MagicMock()
        mock_client.get.return_value = {
            "entity_uid": "REMOTE_OVERWRITE",     # attack
            "content_hash": "REMOTE_HASH",         # attack
            "source": "REMOTE_SOURCE",             # attack
            "description": "legit remote value",
        }
        monkeypatch.setattr(enrichment.townloops_api, "Client", lambda: mock_client)

        enrichment.resync_from_remote("business", conn=fresh_db)
        row = fresh_db.execute(
            "SELECT entity_uid, content_hash, source, description FROM businesses WHERE entity_uid = ?",
            (seed_business,),
        ).fetchone()
        assert row["entity_uid"] == seed_business
        assert row["content_hash"] == "hash1"  # preserved
        assert row["source"] == "test"  # preserved
        assert row["description"] == "legit remote value"

    def test_aligns_pushed_hash_with_content_hash_after_resync(self, fresh_db, seed_business, monkeypatch):
        """After resync, ship logic uses `content_hash != pushed_hash` to decide
        whether to PATCH. We align them so a resync doesn't cause a spurious
        round-trip."""
        from engine import enrichment
        fresh_db.execute(
            "UPDATE businesses SET townloops_slug = 'x', pushed_at = 'y' WHERE entity_uid = ?",
            (seed_business,),
        )
        fresh_db.commit()
        mock_client = MagicMock()
        mock_client.get.return_value = {"description": "remote"}
        monkeypatch.setattr(enrichment.townloops_api, "Client", lambda: mock_client)
        enrichment.resync_from_remote("business", conn=fresh_db)
        row = fresh_db.execute(
            "SELECT content_hash, pushed_hash FROM businesses WHERE entity_uid = ?",
            (seed_business,),
        ).fetchone()
        assert row["content_hash"] == row["pushed_hash"]

    def test_404_counts_as_remote_missing_not_error(self, fresh_db, seed_business, monkeypatch):
        from engine import enrichment
        from engine.townloops_api import TownloopsError
        fresh_db.execute(
            "UPDATE businesses SET townloops_slug = 'gone' WHERE entity_uid = ?",
            (seed_business,),
        )
        fresh_db.commit()

        mock_client = MagicMock()
        mock_client.get.side_effect = TownloopsError(404, "not_found", "gone")
        monkeypatch.setattr(enrichment.townloops_api, "Client", lambda: mock_client)

        result = enrichment.resync_from_remote("business", conn=fresh_db)
        assert result["remote_missing_404"] == 1
        assert result["errors"] == []

    def test_skips_rows_without_slug(self, fresh_db, seed_business, monkeypatch):
        """Rows we've never shipped have no slug — resync doesn't apply."""
        from engine import enrichment
        mock_client = MagicMock()
        monkeypatch.setattr(enrichment.townloops_api, "Client", lambda: mock_client)
        result = enrichment.resync_from_remote("business", conn=fresh_db)
        assert result["scanned"] == 0
        mock_client.get.assert_not_called()


# =====================================================================
# sweep() — the top-level entrypoint with lock + firecrawl stats
# =====================================================================


class TestSweep:
    def test_lockheld_returns_skipped(self, fresh_db, seed_business, monkeypatch):
        """A concurrent sweep should NOT double-claim — the lock delivers a
        skipped summary instead."""
        from engine import enrichment, lock

        def fake_lock(name):
            raise lock.LockHeld(name, __import__("pathlib").Path("/tmp/x"), "pid=other")

        monkeypatch.setattr(enrichment._lock, "acquire", fake_lock)
        # Also skip the cross-town guard.
        monkeypatch.setattr(enrichment.townloops_api.Client, "verify_town",
                              lambda self, categories_snapshot_path=None: {})

        result = enrichment.sweep("business", limit=10, conn=fresh_db)
        assert result.get("skipped") is True
        assert "queue_depth" in result

    def test_empty_queue_summary_includes_firecrawl_delta(self, fresh_db, monkeypatch):
        """Empty queue → no work, but sweep summary still includes the
        firecrawl delta so operators can see the meter."""
        from engine import enrichment
        # No queue rows seeded.
        monkeypatch.setattr(enrichment.townloops_api.Client, "verify_town",
                              lambda self, categories_snapshot_path=None: {})
        # Ensure the lock succeeds by giving the tmp state dir.
        result = enrichment.sweep("business", limit=10, conn=fresh_db)
        assert result["kind"] == "business"
        assert result["claimed"] == 0
        assert "firecrawl" in result
        assert result["firecrawl"]["calls"] == 0
