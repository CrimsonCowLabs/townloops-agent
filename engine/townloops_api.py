"""HTTP client for the TownLoops Ingest API.

Env:
  TOWNLOOPS_API_URL  base URL (e.g. http://localhost:8004)
  TOWNLOOPS_API_KEY  bearer token, town × role scoped
  TOWNLOOPS_TOWN_SLUG  which town this deployment services (single-valued)

All responses unwrap the `{data, warnings}` envelope. Errors raise TownloopsError
carrying the parsed `{error: {code, message, details}}` from the server.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Iterable, Optional

import requests


class TownloopsError(RuntimeError):
    def __init__(self, status: int, code: str, message: str, details: Any = None):
        super().__init__(f"[{status} {code}] {message}")
        self.status = status
        self.code = code
        self.message = message
        self.details = details


# --------------------------------------------------------------------------
# Freeze guard — set by the townloops-api skill when a breaking API change
# is detected. Refuses writes on affected resources until an operator clears
# state/api_freeze.json.
#
# Cached per-process with an mtime check so a `check` run inside the same
# python process (rare, but not impossible) is picked up on the next write.
# --------------------------------------------------------------------------

def _freeze_path() -> Path:
    return Path(os.environ.get(
        "TOWNLOOPS_API_FREEZE_PATH",
        os.path.join(
            os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")),
            "state", "api_freeze.json",
        ),
    ))


_FREEZE_CACHE: dict = {"path": None, "mtime": -1.0, "resources": frozenset()}


def _frozen_resources() -> frozenset[str]:
    p = _freeze_path()
    try:
        mtime = p.stat().st_mtime
    except OSError:
        _FREEZE_CACHE.update(path=str(p), mtime=-1.0, resources=frozenset())
        return frozenset()
    if _FREEZE_CACHE["path"] == str(p) and _FREEZE_CACHE["mtime"] == mtime:
        return _FREEZE_CACHE["resources"]
    try:
        data = json.loads(p.read_text())
        rs = frozenset(data.get("resources") or [])
    except (OSError, json.JSONDecodeError):
        rs = frozenset()
    _FREEZE_CACHE.update(path=str(p), mtime=mtime, resources=rs)
    return rs


def _guard_write(resource: str) -> None:
    """Raise TownloopsError(resource_frozen) if this resource is frozen."""
    if resource in _frozen_resources():
        raise TownloopsError(
            0, "resource_frozen",
            f"resource {resource!r} is frozen by state/api_freeze.json — "
            "TownLoops API had a breaking change. Update payload builders, "
            "then remove the resource from api_freeze.json (or delete the file "
            "if all resources are safe).",
        )


class Client:
    RESOURCES = (
        "events",
        "venues",
        "businesses",
        "communities",
        "performers",
        "creators",
        "categories",
        "sponsors",
        "ad-slots",       # dash in URL segment; matches OpenAPI /towns/{slug}/ad-slots
    )

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        town_slug: Optional[str] = None,
        timeout: float = 30.0,
    ):
        self.base_url = (base_url or os.environ["TOWNLOOPS_API_URL"]).rstrip("/")
        self.api_key = api_key or os.environ["TOWNLOOPS_API_KEY"]
        self.town_slug = town_slug or os.environ["TOWNLOOPS_TOWN_SLUG"]
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
                "User-Agent": "townloops-agent/1.0",
            }
        )
        self.timeout = timeout

    # --- primitives -----------------------------------------------------

    def _url(self, resource: str, slug: Optional[str] = None) -> str:
        base = f"{self.base_url}/api/v1/towns/{self.town_slug}/{resource}"
        return f"{base}/{slug}" if slug else base

    def _request(
        self,
        method: str,
        url: str,
        *,
        json_body: Any = None,
        params: dict | None = None,
        idempotency_key: str | None = None,
        max_retries: int = 3,
    ) -> Any:
        headers: dict[str, str] = {}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        last_err: Exception | None = None
        for attempt in range(max_retries):
            try:
                r = self.session.request(
                    method,
                    url,
                    json=json_body,
                    params=params,
                    headers=headers,
                    timeout=self.timeout,
                )
            except requests.RequestException as e:
                last_err = e
                time.sleep(0.5 * (2 ** attempt))
                continue
            if r.status_code in (429, 502, 503, 504):
                time.sleep(0.5 * (2 ** attempt))
                continue
            try:
                body = r.json() if r.content else {}
            except ValueError:
                body = {}
            if r.status_code >= 400:
                err = body.get("error", {}) if isinstance(body, dict) else {}
                raise TownloopsError(
                    r.status_code,
                    err.get("code", "http_error"),
                    err.get("message", r.text[:200]),
                    err.get("details"),
                )
            # 2xx: unwrap {data, warnings}
            if isinstance(body, dict) and "data" in body:
                return body["data"]
            return body
        raise TownloopsError(0, "network", f"exhausted retries: {last_err}")

    # --- health ---------------------------------------------------------

    def healthz(self) -> Any:
        return self._request("GET", f"{self.base_url}/api/v1/healthz")

    # --- cross-town guard ----------------------------------------------
    # Per-process cache — keyed by (base_url, town_slug). Once the guard
    # passes for a town, subsequent calls are free.
    _VERIFIED: set[tuple[str, str]] = set()

    def verify_town(self, categories_snapshot_path: Optional[str] = None) -> dict:
        """Fail-fast if the configured $TOWNLOOPS_TOWN_SLUG isn't what we think.

        Checks:
          1. `list("categories")` returns a non-empty result for this slug.
             Typo in slug → 404 → TownloopsError. Wrong token → 401/403.
          2. If a categories snapshot exists (written by `town-init setup`),
             its slug set must be a subset of the live category slugs. Extra
             live slugs are fine (categories may be added over time); extra
             *snapshot* slugs mean the deployment's DB was populated against
             a different town's taxonomy.

        Callers should invoke at the top of any long-running or cron-fired
        entry point (event-ingest, enrichment sweep). Once verified, cheap.
        """
        cache_key = (self.base_url, self.town_slug)
        if cache_key in Client._VERIFIED:
            return {"cached": True, "town_slug": self.town_slug}

        live = self.list("categories", limit=200) or []
        if not live:
            raise TownloopsError(
                0, "empty_categories",
                f"town {self.town_slug!r} returned no categories — slug typo "
                "or brand-new town. Refusing to write to an uninitialised town.",
            )
        live_slugs = {c.get("slug") for c in live if isinstance(c, dict) and c.get("slug")}

        # Categories snapshot from town-init setup.
        snap_path = Path(categories_snapshot_path or os.path.join(
            os.environ.get("HERMES_HOME", "/home/agent/.hermes"),
            "state", "categories.json",
        ))
        drift: list[str] = []
        if snap_path.exists():
            try:
                snapshot = json.loads(snap_path.read_text())
                snap_slugs = {c.get("slug") for c in snapshot
                               if isinstance(c, dict) and c.get("slug")}
                drift = sorted(snap_slugs - live_slugs)
            except (json.JSONDecodeError, OSError):
                drift = []
        if drift:
            raise TownloopsError(
                0, "cross_town_drift",
                f"categories snapshot has slugs the live API for town "
                f"{self.town_slug!r} does NOT have: {drift}. This deployment's "
                "state was populated against a different town. Refusing to "
                "proceed. Fix: `rm state/categories.json` and re-run "
                "`town-init setup` if you're INTENTIONALLY changing towns.",
            )
        Client._VERIFIED.add(cache_key)
        return {
            "cached": False,
            "town_slug": self.town_slug,
            "live_categories": len(live_slugs),
            "snapshot_seen": snap_path.exists(),
        }

    # --- generic CRUD --------------------------------------------------

    def list(self, resource: str, **params) -> list[dict]:
        return self._request("GET", self._url(resource), params=params) or []

    def paginate(self, resource: str, page_size: int = 200, **params) -> Iterable[dict]:
        offset = 0
        while True:
            page = self.list(resource, limit=page_size, offset=offset, **params)
            if not page:
                return
            for row in page:
                yield row
            if len(page) < page_size:
                return
            offset += page_size

    def get(self, resource: str, slug: str) -> dict:
        return self._request("GET", self._url(resource, slug))

    def create(self, resource: str, payload: dict, *, idempotency_key: str | None = None) -> dict:
        _guard_write(resource)
        return self._request(
            "POST",
            self._url(resource),
            json_body=payload,
            idempotency_key=idempotency_key,
        )

    def update(self, resource: str, slug: str, payload: dict) -> dict:
        _guard_write(resource)
        return self._request("PATCH", self._url(resource, slug), json_body=payload)

    def delete(self, resource: str, slug: str) -> None:
        _guard_write(resource)
        self._request("DELETE", self._url(resource, slug))

    # --- polymorphic bulk ingest ---------------------------------------
    # bulk_entries() was defined here but never called — event-ingest's ship
    # loop uses raw requests.post with its own retry + Idempotency-Key + 207
    # Multi-Status handling. Deleted 2026-07-08 to remove the dead export.
    # If bulk becomes shared logic, rebuild it here with all three pieces
    # (retry, idempotency-key, 207 unwrap) rather than reviving the stub.
