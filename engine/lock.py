"""Advisory file lock for long-running cron jobs.

The Hermes scheduler already serializes parallel cron triggers via
`max_parallel_jobs: 1` in config.yaml, but that gates *new* jobs. It does
NOT stop tomorrow's 09:00 fire from starting while last night's 09:00 job
is still stuck at hour 22. This lock does.

Usage inside a skill's cron entry point:

    from engine.lock import acquire, LockHeld
    try:
        with acquire("event-ingest"):
            do_the_long_thing()
    except LockHeld as e:
        sys.stderr.write(f"[event-ingest] previous run still active: {e}\\n")
        sys.exit(0)   # exit 0 — this is expected under overlap, not a failure

Backing store: $TOWNLOOPS_LOCK_DIR (default $HERMES_HOME/state/locks/, or
/tmp/townloops-locks if HERMES_HOME is unset). Lock is `fcntl.flock`
LOCK_EX | LOCK_NB — held for the lifetime of the context manager; released
even on process crash (kernel-level).
"""

from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


class LockHeld(RuntimeError):
    def __init__(self, name: str, path: Path, holder: str):
        super().__init__(f"lock {name!r} held by {holder}")
        self.name = name
        self.path = path
        self.holder = holder


def _lock_dir() -> Path:
    override = os.environ.get("TOWNLOOPS_LOCK_DIR")
    if override:
        return Path(override)
    hermes_home = os.environ.get("HERMES_HOME")
    if hermes_home:
        return Path(hermes_home) / "state" / "locks"
    return Path("/tmp/townloops-locks")


@contextmanager
def acquire(name: str):
    """Acquire an exclusive lock scoped to `name`. Raises LockHeld if another
    process already holds it. On success, records `<pid>|<iso_ts>` inside the
    lockfile for postmortem attribution."""
    base = _lock_dir()
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"{name}.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # Read existing holder metadata for the error message.
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                holder = os.read(fd, 256).decode("utf-8", errors="replace").strip() or "unknown"
            except OSError:
                holder = "unknown"
            raise LockHeld(name, path, holder)
        # Record who holds it for postmortems.
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        stamp = f"{os.getpid()}|{datetime.now(timezone.utc).isoformat(timespec='seconds')}\n"
        os.write(fd, stamp.encode("utf-8"))
        os.fsync(fd)
        yield path
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass
