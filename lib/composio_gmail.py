"""Composio Gmail wrapper, talking directly to the REST API.

We deliberately avoid the `composio-core` SDK because its strict pydantic
models break against the live backend (a Slack trigger schema returned by
Composio is missing field descriptions, which crashes the SDK at import
time even when you only want Gmail).

Using HTTP keeps us unaffected by SDK churn.
"""

from __future__ import annotations

import base64
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any, Iterable, Iterator

import httpx
from bs4 import BeautifulSoup
from dateutil import parser as dateparser

from .utils import with_retry


# ---- normalized message shape ---------------------------------------------


@dataclass
class GmailMessage:
    message_id: str
    thread_id: str
    subject: str
    sender_name: str
    sender_email: str
    to: list[str]
    cc: list[str]
    received_at: datetime
    snippet: str
    body_text: str
    body_html: str
    labels: list[str] = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def participants(self) -> list[str]:
        people = [self.sender_email, *self.to, *self.cc]
        seen, out = set(), []
        for p in people:
            p = (p or "").lower().strip()
            if p and p not in seen:
                seen.add(p)
                out.append(p)
        return out


def _strip_html(html: str) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["style", "script"]):
        tag.decompose()
    text = soup.get_text("\n", strip=True)
    lines = [ln.rstrip() for ln in text.splitlines()]
    out, blank = [], 0
    for ln in lines:
        if not ln.strip():
            blank += 1
            if blank <= 1:
                out.append("")
        else:
            blank = 0
            out.append(ln)
    return "\n".join(out).strip()


def _b64decode(data: str) -> bytes:
    if not data:
        return b""
    pad = 4 - (len(data) % 4)
    if pad and pad < 4:
        data += "=" * pad
    return base64.urlsafe_b64decode(data)


def _walk_parts(payload: dict) -> Iterator[dict]:
    if not payload:
        return
    yield payload
    for child in payload.get("parts", []) or []:
        yield from _walk_parts(child)


def _extract_bodies(payload: dict) -> tuple[str, str]:
    text, html = "", ""
    for part in _walk_parts(payload):
        mime = part.get("mimeType", "")
        body = part.get("body") or {}
        data = body.get("data", "")
        if not data:
            continue
        try:
            decoded = _b64decode(data).decode("utf-8", errors="replace")
        except Exception:
            continue
        if mime == "text/plain" and not text:
            text = decoded
        elif mime == "text/html" and not html:
            html = decoded
    if not text and html:
        text = _strip_html(html)
    return text, html


def _parse_address(raw: str) -> tuple[str, str]:
    name, addr = parseaddr(raw or "")
    return name.strip(), addr.strip().lower()


def _parse_date(raw: str) -> datetime:
    if not raw:
        return datetime.now(timezone.utc)
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        try:
            dt = dateparser.parse(raw)
        except Exception:
            dt = datetime.now(timezone.utc)
    if dt is None:
        dt = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def normalize(raw: dict) -> GmailMessage:
    """Convert a Gmail-API-shaped message dict into a GmailMessage.

    Composio sometimes returns:
      - the raw Gmail API shape ({id, payload: {headers,...}, ...}),
      - a flattened shape ({messageId, subject, sender, messageText, ...}),
      - or wraps either inside {data: {...}}.
    Handle all three.
    """
    if "data" in raw and isinstance(raw["data"], dict) and "payload" not in raw:
        raw = raw["data"]

    # Flattened Composio shape
    if "messageText" in raw or "preview" in raw or "messageTimestamp" in raw:
        sender_name, sender_email = _parse_address(raw.get("sender", "") or raw.get("from", ""))
        to_raw = raw.get("to") or []
        if isinstance(to_raw, str):
            to_raw = [a.strip() for a in to_raw.split(",") if a.strip()]
        cc_raw = raw.get("cc") or []
        if isinstance(cc_raw, str):
            cc_raw = [a.strip() for a in cc_raw.split(",") if a.strip()]
        body_text = raw.get("messageText") or raw.get("body") or raw.get("preview", {}).get("body", "")
        body_html = raw.get("messageHtml") or raw.get("messageBody") or ""
        if not body_text and body_html:
            body_text = _strip_html(body_html)
        received = raw.get("messageTimestamp") or raw.get("date") or raw.get("internalDate")
        if isinstance(received, (int, float)) or (isinstance(received, str) and received.isdigit()):
            received_at = datetime.fromtimestamp(int(received) / 1000, tz=timezone.utc)
        else:
            received_at = _parse_date(received or "")
        return GmailMessage(
            message_id=raw.get("messageId") or raw.get("id") or "",
            thread_id=raw.get("threadId") or "",
            subject=raw.get("subject", "") or "(no subject)",
            sender_name=sender_name,
            sender_email=sender_email,
            to=[parseaddr(a)[1].lower() for a in to_raw],
            cc=[parseaddr(a)[1].lower() for a in cc_raw],
            received_at=received_at,
            snippet=(raw.get("preview", {}) or {}).get("snippet", "") if isinstance(raw.get("preview"), dict) else (raw.get("snippet") or ""),
            body_text=body_text,
            body_html=body_html,
            labels=raw.get("labelIds") or raw.get("labels") or [],
            headers={},
        )

    # Raw Gmail API shape
    payload = raw.get("payload") or {}
    headers = {h.get("name", "").lower(): h.get("value", "") for h in payload.get("headers", []) or []}
    sender_name, sender_email = _parse_address(headers.get("from", ""))
    to = [a.strip() for a in (headers.get("to", "").split(",")) if a.strip()]
    cc = [a.strip() for a in (headers.get("cc", "").split(",")) if a.strip()]
    body_text, body_html = _extract_bodies(payload)
    return GmailMessage(
        message_id=raw.get("id") or raw.get("messageId") or "",
        thread_id=raw.get("threadId") or "",
        subject=headers.get("subject", "(no subject)"),
        sender_name=sender_name,
        sender_email=sender_email,
        to=[parseaddr(a)[1].lower() for a in to],
        cc=[parseaddr(a)[1].lower() for a in cc],
        received_at=_parse_date(headers.get("date", "")),
        snippet=(raw.get("snippet") or "").strip(),
        body_text=body_text,
        body_html=body_html,
        labels=raw.get("labelIds", []) or [],
        headers=headers,
    )


# ---- HTTP client -----------------------------------------------------------


class ComposioError(RuntimeError):
    pass


class ComposioGmail:
    """Thin REST client for Composio Gmail actions."""

    def __init__(
        self,
        api_key: str | None = None,
        entity_id: str = "default",
        base_url: str | None = None,
        timeout: float = 60.0,
    ):
        self.api_key = api_key or os.getenv("COMPOSIO_API_KEY", "")
        if not self.api_key:
            raise ComposioError("COMPOSIO_API_KEY missing")
        self.entity_id = entity_id
        self.base_url = (base_url or os.getenv("COMPOSIO_BASE_URL", "https://backend.composio.dev")).rstrip("/")
        self.client = httpx.Client(
            base_url=self.base_url,
            headers={
                "x-api-key": self.api_key,
                "accept": "application/json",
                "content-type": "application/json",
                "user-agent": "n8n-meeting-memory/0.1",
            },
            timeout=timeout,
        )

    # ---- raw helpers ------------------------------------------------------

    def _get(self, path: str, params: dict | None = None) -> Any:
        def _call():
            r = self.client.get(path, params=params or {})
            r.raise_for_status()
            return r.json()
        try:
            return with_retry(_call)
        except httpx.HTTPStatusError as exc:
            raise ComposioError(
                f"GET {path} -> {exc.response.status_code}: {exc.response.text[:400]}"
            ) from exc
        except httpx.TransportError as exc:
            raise ComposioError(f"GET {path}: network error after retries: {exc}") from exc

    def _post(self, path: str, body: dict) -> Any:
        def _call():
            r = self.client.post(path, json=body)
            r.raise_for_status()
            return r.json()
        try:
            return with_retry(_call)
        except httpx.HTTPStatusError as exc:
            raise ComposioError(
                f"POST {path} -> {exc.response.status_code}: {exc.response.text[:400]}"
            ) from exc
        except httpx.TransportError as exc:
            raise ComposioError(f"POST {path}: network error after retries: {exc}") from exc

    # ---- introspection ----------------------------------------------------

    def list_gmail_connections(self, statuses: list[str] | None = None) -> list[dict]:
        """Return Gmail connected_accounts (any user) for the project."""
        params: dict = {"toolkit_slugs": "gmail"}
        if self.entity_id:
            params["user_ids"] = self.entity_id
        if statuses:
            params["statuses"] = ",".join(statuses)
        data = self._get("/api/v3.1/connected_accounts", params=params)
        items = data.get("items") or data.get("data") or []
        return list(items) if isinstance(items, list) else []

    def find_or_create_gmail_auth_config(self) -> str:
        """Return an auth_config_id for Gmail. Creates a Composio-managed one
        if none exists for this project."""
        # Try existing
        try:
            data = self._get(
                "/api/v3.1/auth_configs",
                params={"toolkit_slug": "gmail", "is_composio_managed": "true", "limit": 50},
            )
            items = data.get("items") or data.get("data") or []
            for it in items:
                if str((it.get("toolkit") or {}).get("slug", "")).lower() == "gmail":
                    return it.get("id") or it.get("nano_id") or it.get("nanoid")
        except ComposioError:
            pass
        # Create one
        body = {
            "toolkit": {"slug": "gmail"},
            "auth_config": {"type": "use_composio_managed_auth"},
        }
        resp = self._post("/api/v3.1/auth_configs", body)
        cfg = resp.get("auth_config") or resp.get("data", {}).get("auth_config") or resp
        cid = cfg.get("id") or cfg.get("nano_id") or cfg.get("nanoid")
        if not cid:
            raise ComposioError(f"could not parse auth_config_id from response: {resp}")
        return cid

    def initiate_gmail_oauth(self, callback_url: str | None = None) -> dict:
        """Create a v3.1 link session. Returns the LinkCreateResponse dict
        (redirect_url, connected_account_id, link_token, expires_at)."""
        auth_config_id = self.find_or_create_gmail_auth_config()
        body: dict = {"auth_config_id": auth_config_id, "user_id": self.entity_id}
        if callback_url:
            body["callback_url"] = callback_url
        return self._post("/api/v3.1/connected_accounts/link", body)

    def get_connection_status(self, connected_account_id: str) -> dict:
        return self._get(f"/api/v3.1/connected_accounts/{connected_account_id}/status")

    # ---- action execution -------------------------------------------------

    def _execute(self, tool_slug: str, arguments: dict) -> dict:
        body = {"arguments": arguments, "user_id": self.entity_id}
        resp = self._post(f"/api/v3.1/tools/execute/{tool_slug}", body)
        if resp.get("successful") is False:
            err = resp.get("error") or resp.get("message") or "execute failed"
            raise ComposioError(f"{tool_slug}: {err}")
        return resp.get("data") or resp.get("response_data") or resp

    # ---- public Gmail API ------------------------------------------------

    def search_message_ids(self, query: str, max_results: int = 200) -> list[str]:
        ids: list[str] = []
        page_token: str | None = None
        per_page = min(max_results, 100)
        while len(ids) < max_results:
            args: dict = {"query": query, "max_results": per_page, "user_id": "me"}
            if page_token:
                args["page_token"] = page_token
            data = self._execute("GMAIL_FETCH_EMAILS", args)
            # Composio v3.1 may return at top level OR nested under data
            inner = data.get("data") if isinstance(data.get("data"), dict) else data
            msgs = (
                inner.get("messages")
                or inner.get("response_data", {}).get("messages")
                or []
            )
            for m in msgs:
                mid = m.get("messageId") or m.get("id")
                if mid:
                    ids.append(mid)
                if len(ids) >= max_results:
                    break
            page_token = inner.get("nextPageToken") or inner.get("next_page_token")
            if not page_token:
                break
        return ids

    def fetch_message(self, message_id: str) -> GmailMessage:
        data = self._execute(
            "GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID",
            {"message_id": message_id, "user_id": "me", "format": "full"},
        )
        # Composio sometimes returns {data: {...gmail message...}}
        if isinstance(data.get("data"), dict) and ("payload" in data["data"] or "messageText" in data["data"]):
            data = data["data"]
        return normalize(data)

    def iter_messages(
        self,
        query: str,
        max_results: int = 200,
        sleep: float = 0.05,
    ) -> Iterable[GmailMessage]:
        for mid in self.search_message_ids(query=query, max_results=max_results):
            try:
                yield self.fetch_message(mid)
            except Exception as exc:  # pragma: no cover - network
                print(f"[composio_gmail] failed to fetch {mid}: {exc}")
            time.sleep(sleep)
