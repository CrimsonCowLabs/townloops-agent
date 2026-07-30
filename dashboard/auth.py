"""Password + session auth for the operator dashboard.

Password: bcrypt hash in DASHBOARD_PASSWORD env var. Generate via
`python -m dashboard.auth hash` (reads from stdin).

Session: itsdangerous-signed cookie carrying {"iat": <unix>}. HMAC key
lives at $HERMES_HOME/state/dashboard_secret — auto-generated on first
boot (32 random bytes, mode 0600) so operators don't have to configure
one. Losing that file invalidates all outstanding sessions; the operator
just re-logs-in.
"""

from __future__ import annotations

import getpass
import os
import secrets
import sys
from pathlib import Path
from typing import Optional

import bcrypt
from fastapi import Cookie, HTTPException, Request, status
from itsdangerous import BadSignature, SignatureExpired, TimestampSigner


_COOKIE_NAME = "tl_session"
_COOKIE_MAX_AGE = 60 * 60 * 24 * 30  # 30 days
_SECRET_FILE = Path(
    os.environ.get("HERMES_HOME", "/home/agent/.hermes")
) / "state" / "dashboard_secret"


def _load_secret() -> bytes:
    """Read or auto-generate the HMAC secret. Written 0600 on first call."""
    if _SECRET_FILE.exists():
        return _SECRET_FILE.read_bytes()
    _SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(32)
    _SECRET_FILE.write_bytes(key)
    os.chmod(_SECRET_FILE, 0o600)
    return key


def _signer() -> TimestampSigner:
    return TimestampSigner(_load_secret())


def verify_password(plain: str) -> bool:
    """Compare `plain` against the bcrypt hash in DASHBOARD_PASSWORD.

    Returns False if the env var is missing/empty (operator hasn't
    bootstrapped yet — refuse all logins rather than allow anonymous).
    """
    stored = os.environ.get("DASHBOARD_PASSWORD", "").strip()
    if not stored:
        return False
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), stored.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def issue_session_cookie() -> tuple[str, str, dict]:
    """Return (name, value, extra_kwargs) for FastAPI response.set_cookie."""
    token = _signer().sign(b"ok").decode("utf-8")
    kwargs = {
        "httponly": True,
        "samesite": "strict",
        "max_age": _COOKIE_MAX_AGE,
        "secure": os.environ.get("DASHBOARD_COOKIE_SECURE", "false").lower()
                    in ("true", "1", "yes"),
    }
    return _COOKIE_NAME, token, kwargs


def clear_session_cookie() -> tuple[str, dict]:
    return _COOKIE_NAME, {
        "httponly": True,
        "samesite": "strict",
    }


def require_session(request: Request,
                    tl_session: Optional[str] = Cookie(default=None)) -> None:
    """FastAPI dependency: raise 401 unless a valid session cookie is present."""
    if not tl_session:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="login required",
                            headers={"Location": "/login"})
    try:
        _signer().unsign(tl_session, max_age=_COOKIE_MAX_AGE)
    except (BadSignature, SignatureExpired) as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail=f"session invalid: {exc}",
                            headers={"Location": "/login"})


# --------------------------------------------------------------------------
# Hash CLI: `python -m dashboard.auth hash`
# --------------------------------------------------------------------------

def _hash_cmd() -> int:
    if sys.stdin.isatty():
        pw = getpass.getpass("Dashboard password: ")
    else:
        pw = sys.stdin.read().rstrip("\n")
    if not pw:
        print("empty password rejected", file=sys.stderr)
        return 2
    hashed = bcrypt.hashpw(pw.encode("utf-8"), bcrypt.gensalt(rounds=12))
    print(hashed.decode("utf-8"))
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] != "hash":
        print("usage: python -m dashboard.auth hash", file=sys.stderr)
        return 2
    return _hash_cmd()


if __name__ == "__main__":
    sys.exit(main())
