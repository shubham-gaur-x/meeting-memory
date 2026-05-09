"""Write meeting notes + person notes into the Obsidian vault."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import yaml

from .composio_gmail import GmailMessage
from .extractor import Extracted


SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._\- ]+")


def _safe(name: str, max_len: int = 80) -> str:
    s = SAFE_FILENAME_RE.sub("", (name or "").strip())
    s = re.sub(r"\s+", " ", s).strip()
    return s[:max_len].rstrip(" .") or "untitled"


def _yaml_block(d: dict) -> str:
    return "---\n" + yaml.safe_dump(d, sort_keys=False, allow_unicode=True).strip() + "\n---\n"


def _ensure_dirs(vault: Path) -> None:
    for sub in ["Meetings", "People", "Topics", "_Dashboards", "_Templates"]:
        (vault / sub).mkdir(parents=True, exist_ok=True)


def _person_filename(person: dict) -> str | None:
    name = (person.get("name") or "").strip()
    email = (person.get("email") or "").strip().lower()
    if name:
        return _safe(name)
    if email:
        return _safe(email.split("@")[0])
    return None


def _link(target: str) -> str:
    return f"[[{target}]]"


def write_meeting(vault: Path, msg: GmailMessage, ex: Extracted) -> Path:
    _ensure_dirs(vault)

    meeting_date = ex.date or msg.received_at.date().isoformat()
    title = _safe(ex.title or msg.subject or "Meeting")
    fname = f"{meeting_date} - {title}.md"
    path = vault / "Meetings" / fname

    attendee_links: list[str] = []
    for a in ex.attendees:
        pname = _person_filename(a)
        if pname:
            attendee_links.append(_link(f"People/{pname}"))

    fm = {
        "type": "meeting",
        "date": meeting_date,
        "start_time": ex.start_time,
        "end_time": ex.end_time,
        "duration_minutes": ex.duration_minutes,
        "platform": ex.platform,
        "kind": ex.kind,
        "location": ex.location,
        "attendees": [
            {"name": a.get("name"), "email": a.get("email"), "role": a.get("role")}
            for a in ex.attendees
        ],
        "topics": ex.topics,
        "decisions": ex.decisions,
        "action_items": ex.action_items,
        "sentiment": ex.sentiment,
        "follow_up_needed": ex.follow_up_needed,
        "confidence": ex.confidence,
        "links": ex.links,
        "source": {
            "message_id": msg.message_id,
            "thread_id": msg.thread_id,
            "subject": msg.subject,
            "from": f"{msg.sender_name} <{msg.sender_email}>".strip(),
            "received_at": msg.received_at.isoformat(),
            "labels": msg.labels,
        },
    }

    body_lines: list[str] = []
    body_lines.append(f"# {ex.title or msg.subject}")
    body_lines.append("")
    if ex.summary:
        body_lines.append("## Summary")
        body_lines.append(ex.summary)
        body_lines.append("")

    if attendee_links:
        body_lines.append("## Attendees")
        body_lines.extend(f"- {l}" for l in attendee_links)
        body_lines.append("")

    if ex.decisions:
        body_lines.append("## Decisions")
        body_lines.extend(f"- {d}" for d in ex.decisions)
        body_lines.append("")

    if ex.action_items:
        body_lines.append("## Action items")
        for ai in ex.action_items:
            owner = ai.get("owner") or "—"
            task = ai.get("task") or ""
            due = ai.get("due") or ""
            check = "x" if ai.get("done") else " "
            line = f"- [{check}] **{owner}** — {task}"
            if due:
                line += f"  *(due {due})*"
            body_lines.append(line)
        body_lines.append("")

    if ex.topics:
        body_lines.append("## Topics")
        body_lines.append(" ".join(f"#{t}" for t in ex.topics))
        body_lines.append("")

    if ex.key_quotes:
        body_lines.append("## Key quotes")
        body_lines.extend(f"> {q}" for q in ex.key_quotes)
        body_lines.append("")

    if ex.links:
        body_lines.append("## Links")
        body_lines.extend(f"- {u}" for u in ex.links)
        body_lines.append("")

    body_lines.append("## Source email")
    body_lines.append(f"- **Subject:** {msg.subject}")
    body_lines.append(f"- **From:** {msg.sender_name} <{msg.sender_email}>")
    body_lines.append(f"- **Received:** {msg.received_at.isoformat()}")
    body_lines.append(f"- **Gmail message id:** `{msg.message_id}`")
    body_lines.append("")
    body_lines.append("<details><summary>Original body</summary>")
    body_lines.append("")
    body_lines.append("```")
    body_lines.append((msg.body_text or "").strip())
    body_lines.append("```")
    body_lines.append("")
    body_lines.append("</details>")

    path.write_text(_yaml_block(fm) + "\n" + "\n".join(body_lines) + "\n", encoding="utf-8")

    # Upsert person notes for each attendee
    for a in ex.attendees:
        upsert_person(vault, a, meeting_path=path, meeting_title=ex.title or msg.subject, date=meeting_date)

    return path


def upsert_person(
    vault: Path,
    person: dict,
    *,
    meeting_path: Path,
    meeting_title: str,
    date: str,
) -> Path | None:
    pname = _person_filename(person)
    if not pname:
        return None
    p = vault / "People" / f"{pname}.md"
    note_link = f"[[Meetings/{meeting_path.stem}|{date} — {meeting_title}]]"

    if not p.exists():
        fm = {
            "type": "person",
            "name": person.get("name") or pname,
            "email": person.get("email"),
            "first_seen": date,
        }
        body = [
            f"# {person.get('name') or pname}",
            "",
            "## Meetings",
            f"- {note_link}",
            "",
            "## Notes",
            "",
        ]
        p.write_text(_yaml_block(fm) + "\n" + "\n".join(body) + "\n", encoding="utf-8")
        return p

    text = p.read_text(encoding="utf-8")
    if note_link in text:
        return p
    if "## Meetings" in text:
        text = text.replace("## Meetings\n", f"## Meetings\n- {note_link}\n", 1)
    else:
        text += f"\n## Meetings\n- {note_link}\n"
    p.write_text(text, encoding="utf-8")
    return p
