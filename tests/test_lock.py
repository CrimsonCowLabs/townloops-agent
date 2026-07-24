"""Tests for engine/lock.py — advisory file locks used by cron jobs.

Contention testing uses os.fork() so both processes contend for the SAME
underlying flock; a thread-based test wouldn't exercise fcntl.
"""

from __future__ import annotations

import os

import pytest


class TestLockAcquisition:
    def test_acquire_and_release(self, tmp_state_dir):
        from engine import lock
        with lock.acquire("test-lock") as path:
            assert path.exists()
            # Holder metadata is written for postmortem attribution.
            contents = path.read_text()
            assert str(os.getpid()) in contents

    def test_reacquire_after_release(self, tmp_state_dir):
        """Once the with-block exits, another acquire on the same name works."""
        from engine import lock
        with lock.acquire("re-test"):
            pass
        with lock.acquire("re-test") as path:
            assert path.exists()

    def test_different_names_do_not_conflict(self, tmp_state_dir):
        from engine import lock
        with lock.acquire("name-a"):
            with lock.acquire("name-b") as p:
                assert p.name == "name-b.lock"

    @pytest.mark.slow
    def test_contention_across_processes_raises_lockheld(self, tmp_state_dir):
        """Two processes cannot hold the same lock — os.fork proves it."""
        from engine import lock

        # Parent acquires and holds; child tries to acquire and should get LockHeld.
        with lock.acquire("contended"):
            pid = os.fork()
            if pid == 0:
                # Child process.
                try:
                    with lock.acquire("contended"):
                        os._exit(0)  # unexpected — should have raised
                except lock.LockHeld:
                    os._exit(42)
                except Exception:
                    os._exit(99)
            else:
                _, status = os.waitpid(pid, 0)
                assert os.WEXITSTATUS(status) == 42, (
                    f"expected exit=42 (LockHeld); got {os.WEXITSTATUS(status)}"
                )

    def test_lockdir_defaults_from_hermes_home(self, monkeypatch, tmp_path):
        from engine import lock
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.delenv("TOWNLOOPS_LOCK_DIR", raising=False)
        assert lock._lock_dir() == tmp_path / "state" / "locks"

    def test_lockdir_falls_back_to_tmp(self, monkeypatch):
        from engine import lock
        monkeypatch.delenv("HERMES_HOME", raising=False)
        monkeypatch.delenv("TOWNLOOPS_LOCK_DIR", raising=False)
        from pathlib import Path
        assert lock._lock_dir() == Path("/tmp/townloops-locks")

    def test_lockdir_override_wins(self, monkeypatch, tmp_path):
        from engine import lock
        monkeypatch.setenv("HERMES_HOME", "/would/be/derived")
        monkeypatch.setenv("TOWNLOOPS_LOCK_DIR", str(tmp_path))
        assert lock._lock_dir() == tmp_path


class TestLockHeldException:
    def test_carries_name_and_holder(self, tmp_state_dir):
        from engine import lock
        # Manually construct a LockHeld to check its shape.
        from pathlib import Path
        exc = lock.LockHeld("foo", Path("/tmp/x"), "pid=123|ts=y")
        assert exc.name == "foo"
        assert exc.holder == "pid=123|ts=y"
        assert "foo" in str(exc)
        assert "pid=123" in str(exc)
