"""engine/orchestrator.py — phase machine.

Handlers are monkey-patched to controlled fake outputs; we only test the
state-transition mechanics, not the individual skill invocations.
"""

from __future__ import annotations

import sqlite3

import pytest


@pytest.fixture
def orch_conn(fresh_db):
    fresh_db.row_factory = sqlite3.Row
    return fresh_db


class TestEnsureState:
    def test_creates_first_row(self, orch_conn):
        from engine import orchestrator
        state = orchestrator.ensure_state_row(orch_conn)
        assert state["phase"] == "bootstrapping"
        assert int(state["tick_count"] or 0) == 0

    def test_idempotent(self, orch_conn):
        from engine import orchestrator
        s1 = orchestrator.ensure_state_row(orch_conn)
        s2 = orchestrator.ensure_state_row(orch_conn)
        assert s1["entered_at"] == s2["entered_at"]

    def test_singleton_enforced(self, orch_conn):
        from engine import orchestrator
        orchestrator.ensure_state_row(orch_conn)
        with pytest.raises(sqlite3.IntegrityError):
            orch_conn.execute(
                "INSERT INTO agent_state (id, entered_at) VALUES (2, '2026-01-01')"
            )


class TestNextPhase:
    def test_progression(self):
        from engine import orchestrator
        assert orchestrator._next_phase("bootstrapping") == "discovering"
        assert orchestrator._next_phase("discovering") == "verifying"
        assert orchestrator._next_phase("shipping") == "steady_state"

    def test_steady_state_terminal(self):
        from engine import orchestrator
        # steady_state stays at steady_state — no further advancement
        assert orchestrator._next_phase("steady_state") == "steady_state"


class TestRunTick:
    def _install_handler(self, monkeypatch, phase: str, advance: bool,
                         quiet: bool, summary: dict = None):
        from engine import orchestrator
        summary = summary or {}
        monkeypatch.setitem(
            orchestrator._HANDLERS, phase,
            lambda conn, notes: (advance, quiet, summary),
        )

    def _seed_phase(self, conn, phase: str, phase_ticks: int = 0,
                    quiet_ticks: int = 0):
        conn.execute(
            "UPDATE agent_state SET phase = ?, phase_ticks = ?, quiet_ticks = ?, "
            "entered_at = '2026-01-01T00:00:00+00:00' WHERE id = 1",
            (phase, phase_ticks, quiet_ticks),
        )
        conn.commit()

    def test_hard_advance_from_verifying(self, orch_conn, monkeypatch):
        from engine import orchestrator
        orchestrator.ensure_state_row(orch_conn)
        self._seed_phase(orch_conn, "verifying")
        # Handler says advance=True (would need real DB counts otherwise).
        self._install_handler(monkeypatch, "verifying", advance=True, quiet=False)
        st = orchestrator.run_tick(conn=orch_conn)
        assert st["phase"] == "researching"
        assert st["prev_phase"] == "verifying"

    def test_quiet_ticks_advance_discovering(self, orch_conn, monkeypatch):
        from engine import orchestrator
        orchestrator.ensure_state_row(orch_conn)
        self._seed_phase(orch_conn, "discovering")
        self._install_handler(monkeypatch, "discovering", advance=False, quiet=True)
        # First two quiet ticks should stay; third advances.
        st = orchestrator.run_tick(conn=orch_conn)
        assert st["phase"] == "discovering", "tick 1 shouldn't advance"
        st = orchestrator.run_tick(conn=orch_conn)
        assert st["phase"] == "discovering", "tick 2 shouldn't advance"
        st = orchestrator.run_tick(conn=orch_conn)
        assert st["phase"] == "verifying", f"tick 3 should advance: {st}"

    def test_activity_resets_quiet_counter(self, orch_conn, monkeypatch):
        from engine import orchestrator
        orchestrator.ensure_state_row(orch_conn)
        self._seed_phase(orch_conn, "discovering")

        # Quiet, quiet, then noisy — counter should reset before hitting 3.
        for i, quiet in enumerate([True, True, False, True]):
            self._install_handler(monkeypatch, "discovering", advance=False, quiet=quiet)
            st = orchestrator.run_tick(conn=orch_conn)
            assert st["phase"] == "discovering", f"iter {i}: {st}"

    def test_force_advance_at_cap(self, orch_conn, monkeypatch):
        from engine import orchestrator
        orchestrator.ensure_state_row(orch_conn)
        self._seed_phase(orch_conn, "verifying",
                         phase_ticks=orchestrator.PHASE_MAX_TICKS - 1)
        self._install_handler(monkeypatch, "verifying", advance=False, quiet=False)
        st = orchestrator.run_tick(conn=orch_conn)
        assert st["forced_advance"]
        assert st["phase"] == "researching"

    def test_steady_state_never_advances(self, orch_conn, monkeypatch):
        from engine import orchestrator
        orchestrator.ensure_state_row(orch_conn)
        self._seed_phase(orch_conn, "steady_state")
        st = orchestrator.run_tick(conn=orch_conn)
        assert st["phase"] == "steady_state"
        assert st.get("prev_phase") is None

    def test_lock_contention_reports_error(self, orch_conn, monkeypatch):
        from engine import orchestrator, lock
        orchestrator.ensure_state_row(orch_conn)
        with lock.acquire("orchestrator"):
            st = orchestrator.run_tick(conn=orch_conn)
            assert "error" in st, st

    def test_handler_exception_captured(self, orch_conn, monkeypatch):
        from engine import orchestrator
        orchestrator.ensure_state_row(orch_conn)
        self._seed_phase(orch_conn, "verifying")

        def boom(conn, notes):
            raise RuntimeError("boom")
        monkeypatch.setitem(orchestrator._HANDLERS, "verifying", boom)
        st = orchestrator.run_tick(conn=orch_conn)
        # Tick should not raise; summary carries the error string.
        assert "handler_error" in st["summary"]


class TestBootstrapSkip:
    def test_skips_when_entities_exist(self, orch_conn, monkeypatch):
        from engine import orchestrator
        orchestrator.ensure_state_row(orch_conn)
        # Seed a business row → bootstrap should skip to steady_state.
        orch_conn.execute(
            "INSERT INTO businesses (entity_uid, source, source_kind, name, "
            "first_seen_at, last_seen_at, content_hash, last_modified_at) "
            "VALUES ('u1', 'test', 'test', 'x', 'now', 'now', 'h', 'now')"
        )
        orch_conn.commit()
        st = orchestrator.run_tick(conn=orch_conn)
        # After bootstrap skip + one advance: phase should be steady_state
        row = orch_conn.execute("SELECT phase FROM agent_state WHERE id = 1").fetchone()
        assert row["phase"] == "steady_state"
