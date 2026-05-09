"""Structured extraction from a meeting email.

Sends the email body to an LLM (Claude by default, Ollama optional) and
asks for a strict JSON object describing the meeting.
"""

from __future__ import annotations

import json
import os
import re
import textwrap
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import httpx

from .composio_gmail import GmailMessage
from .utils import with_retry


SYSTEM_PROMPT = """You analyze meeting-related emails (calendar invites, recap threads, recording links, auto-transcripts from Otter/Fathom/Fireflies/Read.ai, intros, scheduling messages) and return a strict JSON object describing the meeting.

Schema (return ONLY JSON, no prose):
{
  "title": str,                  // short, human-readable, e.g. "Q3 pricing review w/ Alice"
  "kind": "invite"|"recording"|"transcript"|"recap"|"conversation"|"other",
  "platform": "google-meet"|"zoom"|"teams"|"webex"|"chime"|"in-person"|"phone"|"unknown",
  "date": "YYYY-MM-DD"|null,     // when the meeting happened (or will happen)
  "start_time": "HH:MM"|null,    // 24h, local
  "end_time":   "HH:MM"|null,
  "duration_minutes": int|null,
  "location": str|null,          // physical or video URL
  "attendees": [
     { "name": str|null, "email": str|null, "role": "host"|"organizer"|"attendee"|"optional"|null }
  ],
  "summary": str,                // 2-4 sentences, plain prose
  "topics": [str],               // 3-8 short topical tags, lowercase, kebab-case
  "decisions": [str],            // each a single declarative sentence; [] if none
  "action_items": [
     { "owner": str|null, "task": str, "due": "YYYY-MM-DD"|null, "done": false }
  ],
  "key_quotes": [str],           // up to 3 short quotes (<25 words each), only if a transcript is included
  "links": [str],                // recording/transcript/doc URLs (deduped)
  "sentiment": "positive"|"neutral"|"mixed"|"tense"|null,
  "follow_up_needed": bool,
  "confidence": float            // 0..1, how sure you are this is really a meeting record
}

Rules:
- If the email is just a scheduling back-and-forth with no confirmed time, kind = "conversation".
- Be conservative: if the email is clearly NOT meeting-related, set confidence < 0.3 and leave most fields empty/null.
- Names should be Title Case. Emails should be lowercase.
- Topics: short (1-3 words), kebab-case, no leading #.
- Action items: if no explicit owner, infer from context; otherwise leave owner null.
- Output JSON only. No markdown fences. No commentary."""


@dataclass
class Extracted:
    title: str
    kind: str
    platform: str
    date: str | None
    start_time: str | None
    end_time: str | None
    duration_minutes: int | None
    location: str | None
    attendees: list[dict] = field(default_factory=list)
    summary: str = ""
    topics: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    action_items: list[dict] = field(default_factory=list)
    key_quotes: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    sentiment: str | None = None
    follow_up_needed: bool = False
    confidence: float = 0.0
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "Extracted":
        return cls(
            title=d.get("title") or "Meeting",
            kind=d.get("kind") or "other",
            platform=d.get("platform") or "unknown",
            date=d.get("date"),
            start_time=d.get("start_time"),
            end_time=d.get("end_time"),
            duration_minutes=d.get("duration_minutes"),
            location=d.get("location"),
            attendees=d.get("attendees") or [],
            summary=d.get("summary") or "",
            topics=[t.lower().strip() for t in (d.get("topics") or [])],
            decisions=d.get("decisions") or [],
            action_items=d.get("action_items") or [],
            key_quotes=d.get("key_quotes") or [],
            links=d.get("links") or [],
            sentiment=d.get("sentiment"),
            follow_up_needed=bool(d.get("follow_up_needed")),
            confidence=float(d.get("confidence") or 0.0),
            raw=d,
        )


# ---- LLM backends ---------------------------------------------------------


def _truncate(s: str, limit: int = 18000) -> str:
    if len(s) <= limit:
        return s
    head = s[: limit // 2]
    tail = s[-limit // 2 :]
    return head + "\n\n…[truncated]…\n\n" + tail


def _build_user_prompt(msg: GmailMessage) -> str:
    return textwrap.dedent(
        f"""\
        SUBJECT: {msg.subject}
        FROM:    {msg.sender_name} <{msg.sender_email}>
        TO:      {", ".join(msg.to)}
        CC:      {", ".join(msg.cc)}
        DATE:    {msg.received_at.isoformat()}
        LABELS:  {", ".join(msg.labels)}

        --- BODY ---
        {_truncate(msg.body_text or "")}
        """
    )


def _strip_json_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
    return text.strip()


def _coerce_json_object(text: str) -> dict:
    """Best-effort recovery: find the first balanced { ... } block."""
    text = _strip_json_fences(text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    if start == -1:
        raise json.JSONDecodeError("no JSON object found", text, 0)
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start : i + 1])
    raise json.JSONDecodeError("unbalanced JSON object", text, start)


def _extract_with_anthropic(msg: GmailMessage) -> dict:
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY missing")
    model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")

    # Lazy import so users on Ollama don't need the SDK
    from anthropic import Anthropic

    client = Anthropic(api_key=api_key)
    user_prompt = _build_user_prompt(msg)

    def _call():
        return client.messages.create(
            model=model,
            max_tokens=2000,
            system=[{
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": user_prompt}],
        )

    resp = with_retry(_call)
    text = "".join(getattr(c, "text", "") for c in resp.content)
    return _coerce_json_object(text)


def _extract_with_ollama(msg: GmailMessage) -> dict:
    host = os.getenv("OLLAMA_HOST", "http://localhost:11434")
    model = os.getenv("OLLAMA_MODEL", "llama3.1:8b")
    payload = {
        "model": model,
        "stream": False,
        "format": "json",
        "system": SYSTEM_PROMPT,
        "prompt": _build_user_prompt(msg),
        "options": {"temperature": 0.2},
    }

    def _call():
        r = httpx.post(f"{host}/api/generate", json=payload, timeout=300)
        r.raise_for_status()
        return r.json()

    data = with_retry(_call)
    text = data.get("response", "{}")
    return _coerce_json_object(text)


def extract(msg: GmailMessage) -> Extracted:
    backend = os.getenv("LLM_BACKEND", "anthropic").lower()
    try:
        if backend == "ollama":
            data = _extract_with_ollama(msg)
        else:
            data = _extract_with_anthropic(msg)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"LLM returned non-JSON: {exc}") from exc
    return Extracted.from_dict(data)
