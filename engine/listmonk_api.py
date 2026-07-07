"""Listmonk HTTP client for weekly newsletter campaigns.

Env:
  LISTMONK_URL     e.g. http://localhost:9090
  LISTMONK_USER    api user
  LISTMONK_TOKEN   token
  LISTMONK_LIST_ID default subscriber list id

Listmonk auth: `Authorization: token <user>:<token>`.
Docs: https://listmonk.app/docs/apis/apis/
"""

from __future__ import annotations

import os
from typing import Any, Optional

import requests


class ListmonkError(RuntimeError):
    pass


class Client:
    def __init__(
        self,
        base_url: Optional[str] = None,
        user: Optional[str] = None,
        token: Optional[str] = None,
        timeout: float = 30.0,
    ):
        self.base_url = (base_url or os.environ["LISTMONK_URL"]).rstrip("/")
        self.user = user or os.environ["LISTMONK_USER"]
        self.token = token or os.environ["LISTMONK_TOKEN"]
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"token {self.user}:{self.token}",
                "Content-Type": "application/json",
                "User-Agent": "townloops-agent/1.0",
            }
        )
        self.timeout = timeout

    def _req(self, method: str, path: str, json_body: Any = None) -> Any:
        r = self.session.request(
            method,
            f"{self.base_url}{path}",
            json=json_body,
            timeout=self.timeout,
        )
        if r.status_code >= 400:
            raise ListmonkError(f"[{r.status_code}] {r.text[:300]}")
        body = r.json() if r.content else {}
        return body.get("data", body) if isinstance(body, dict) else body

    # ---- lists & subscribers ----

    def lists(self) -> list[dict]:
        data = self._req("GET", "/api/lists?per_page=all")
        return data.get("results", data) if isinstance(data, dict) else data

    def get_list(self, list_id: int) -> dict:
        return self._req("GET", f"/api/lists/{list_id}")

    # ---- templates ----

    def templates(self) -> list[dict]:
        return self._req("GET", "/api/templates")

    # ---- campaigns ----

    def create_campaign(
        self,
        *,
        name: str,
        subject: str,
        lists: list[int],
        body: str,
        from_email: Optional[str] = None,
        content_type: str = "html",
        template_id: Optional[int] = None,
    ) -> dict:
        payload: dict[str, Any] = {
            "name": name,
            "subject": subject,
            "lists": lists,
            "type": "regular",
            "content_type": content_type,
            "body": body,
            "messenger": "email",
        }
        if from_email:
            payload["from_email"] = from_email
        if template_id:
            payload["template_id"] = template_id
        return self._req("POST", "/api/campaigns", json_body=payload)

    def update_campaign(self, campaign_id: int, patch: dict) -> dict:
        return self._req("PUT", f"/api/campaigns/{campaign_id}", json_body=patch)

    def schedule_campaign(self, campaign_id: int, send_at_iso: str) -> dict:
        # Listmonk convention: PUT status = scheduled with send_at.
        return self._req(
            "PUT",
            f"/api/campaigns/{campaign_id}/status",
            json_body={"status": "scheduled", "send_at": send_at_iso},
        )

    def get_campaign(self, campaign_id: int) -> dict:
        return self._req("GET", f"/api/campaigns/{campaign_id}")
