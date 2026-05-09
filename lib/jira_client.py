"""Jira Cloud REST API v3 client.

Uses HTTP Basic auth (email:token) — no SDK dependency.
Mirrors the composio_gmail.py pattern: thin httpx wrapper + with_retry.
"""
from __future__ import annotations

import base64
import logging
import os
from typing import Any

import httpx

from .utils import with_retry

logger = logging.getLogger(__name__)


class JiraError(RuntimeError):
    """Raised on non-retryable Jira API failures."""
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class JiraClient:
    """Thin REST client for Jira Cloud API v3."""

    def __init__(
        self,
        domain: str | None = None,   # JIRA_DOMAIN env var, e.g. yourco.atlassian.net
        email: str | None = None,    # JIRA_EMAIL env var
        token: str | None = None,    # JIRA_API_TOKEN env var
        timeout: float = 30.0,
    ):
        self.domain = (domain or os.getenv("JIRA_DOMAIN", "")).rstrip("/")
        if not self.domain:
            raise JiraError("JIRA_DOMAIN missing")
        email = email or os.getenv("JIRA_EMAIL", "")
        token = token or os.getenv("JIRA_API_TOKEN", "")
        if not email or not token:
            raise JiraError("JIRA_EMAIL and JIRA_API_TOKEN are required")

        creds = base64.b64encode(f"{email}:{token}".encode()).decode()
        self._auth_headers = {
            "Authorization": f"Basic {creds}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "meeting-memory-jira/0.1",
        }
        self._timeout = timeout
        self.client = httpx.Client(
            base_url=f"https://{self.domain}/rest/api/3",
            headers=self._auth_headers,
            timeout=timeout,
        )

    def __enter__(self) -> "JiraClient":
        return self

    def __exit__(self, *args: object) -> None:
        self.client.close()

    # ---- internal helpers (mirror composio_gmail.py pattern) ----------------

    def _get(self, path: str, params: dict | None = None) -> Any:
        def _call():
            r = self.client.get(path, params=params or {})
            r.raise_for_status()
            return r.json()
        try:
            return with_retry(_call)
        except httpx.HTTPStatusError as exc:
            _body = exc.response.text[:400]
            # Try to extract Jira's structured error messages
            try:
                err_data = exc.response.json()
                msgs = list((err_data.get("errors") or {}).values())
                msgs += err_data.get("errorMessages") or []
                if msgs:
                    _body = "; ".join(str(m) for m in msgs)
            except Exception:
                pass
            raise JiraError(
                f"GET {path} -> {exc.response.status_code}: {_body}",
                status_code=exc.response.status_code,
            ) from exc
        except httpx.TransportError as exc:
            raise JiraError(f"GET {path}: network error after retries: {exc}") from exc

    def _post(self, path: str, body: dict) -> Any:
        def _call():
            r = self.client.post(path, json=body)
            r.raise_for_status()
            return r.json()
        try:
            return with_retry(_call)
        except httpx.HTTPStatusError as exc:
            _body = exc.response.text[:400]
            try:
                err_data = exc.response.json()
                msgs = list((err_data.get("errors") or {}).values())
                msgs += err_data.get("errorMessages") or []
                if msgs:
                    _body = "; ".join(str(m) for m in msgs)
            except Exception:
                pass
            raise JiraError(
                f"POST {path} -> {exc.response.status_code}: {_body}",
                status_code=exc.response.status_code,
            ) from exc
        except httpx.TransportError as exc:
            raise JiraError(f"POST {path}: network error after retries: {exc}") from exc

    # ---- public API ----------------------------------------------------------

    def get_myself(self) -> dict:
        """GET /myself — validates credentials. Returns current user dict."""
        return self._get("/myself")

    def get_project(self, project_key: str) -> dict:
        """GET /project/{key} — validates project exists."""
        return self._get(f"/project/{project_key}")

    def create_issue(self, fields: dict) -> dict:
        """POST /issue — creates an issue. Returns {key, id, self}."""
        resp = self._post("/issue", {"fields": fields})
        return resp

    def search_users(self, query: str) -> list[dict]:
        """GET /user/search?query= — find users by display name or email."""
        result = self._get("/user/search", params={"query": query, "maxResults": 5})
        return result if isinstance(result, list) else []

    def add_comment(self, issue_key: str, body_adf: dict) -> dict:
        """POST /issue/{key}/comment — add a comment (ADF body)."""
        return self._post(f"/issue/{issue_key}/comment", {"body": body_adf})

    def delete_issue(self, issue_key: str) -> None:
        """DELETE /issue/{key} — permanently deletes an issue."""
        r = self.client.delete(f"/issue/{issue_key}")
        r.raise_for_status()

    # ---- Agile API (sprint management) --------------------------------------
    # Uses /rest/agile/1.0/ — separate base path, called with full URLs.

    def get_active_sprint(self, board_id: str | int) -> dict | None:
        """Return the first active sprint for a board, or None."""
        try:
            r = httpx.get(
                f"https://{self.domain}/rest/agile/1.0/board/{board_id}/sprint",
                headers=self._auth_headers,
                params={"state": "active"},
                timeout=self._timeout,
            )
            r.raise_for_status()
            values = r.json().get("values", [])
            return values[0] if values else None
        except Exception as exc:
            logger.warning("Could not fetch active sprint for board %s: %s", board_id, exc)
            return None

    def add_to_sprint(self, sprint_id: int, issue_keys: list[str]) -> None:
        """Add a batch of issues to a sprint."""
        r = httpx.post(
            f"https://{self.domain}/rest/agile/1.0/sprint/{sprint_id}/issue",
            headers=self._auth_headers,
            json={"issues": issue_keys},
            timeout=self._timeout,
        )
        r.raise_for_status()
