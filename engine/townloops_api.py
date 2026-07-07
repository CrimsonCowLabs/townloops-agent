"""HTTP client for the TownLoops Ingest API.

Env:
  TOWNLOOPS_API_URL  base URL (e.g. http://localhost:8004)
  TOWNLOOPS_API_KEY  bearer token, town × role scoped
  TOWNLOOPS_TOWN_SLUG  which town this deployment services (single-valued)

All responses unwrap the `{data, warnings}` envelope. Errors raise TownloopsError
carrying the parsed `{error: {code, message, details}}` from the server.
"""

from __future__ import annotations

import os
import time
from typing import Any, Iterable, Optional

import requests


class TownloopsError(RuntimeError):
    def __init__(self, status: int, code: str, message: str, details: Any = None):
        super().__init__(f"[{status} {code}] {message}")
        self.status = status
        self.code = code
        self.message = message
        self.details = details


class Client:
    RESOURCES = (
        "events",
        "venues",
        "businesses",
        "communities",
        "performers",
        "creators",
        "categories",
        "sponsors",       # blocked; ready when API endpoints land
        "ad_spots",       # blocked; ready when API endpoints land
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
        return self._request(
            "POST",
            self._url(resource),
            json_body=payload,
            idempotency_key=idempotency_key,
        )

    def update(self, resource: str, slug: str, payload: dict) -> dict:
        return self._request("PATCH", self._url(resource, slug), json_body=payload)

    def delete(self, resource: str, slug: str) -> None:
        self._request("DELETE", self._url(resource, slug))

    # --- polymorphic bulk ingest ---------------------------------------

    def bulk_entries(self, entries: list[dict]) -> dict:
        """POST /entries/bulk — returns {results: [{status, data|error}, ...]}."""
        return self._request(
            "POST",
            f"{self.base_url}/api/v1/towns/{self.town_slug}/entries/bulk",
            json_body={"entries": entries},
        )
