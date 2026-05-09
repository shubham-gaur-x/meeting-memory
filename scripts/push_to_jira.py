#!/usr/bin/env python3
"""Push meeting notes to Jira — standalone backfill script.

Reads state.json + vault notes. Does NOT re-run Gmail or LLM.

Usage:
    python scripts/push_to_jira.py                     # push all unpushed
    python scripts/push_to_jira.py --dry-run            # preview only
    python scripts/push_to_jira.py --message-id <id>   # push one specific note
    python scripts/push_to_jira.py --since 2026-05-01  # filter by note date
    python scripts/push_to_jira.py --force             # re-push already-pushed
    python scripts/push_to_jira.py --create-decisions  # include decisions
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent.parent  # repo root
sys.path.insert(0, str(HERE))

from lib.extractor import Extracted
from lib.jira_client import JiraClient, JiraError
from lib.jira_pusher import push_meeting
from lib.state import State

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

STATE_FILE = HERE / "state.json"


def _load_env() -> None:
    """Load .env file from repo root if present."""
    env_file = HERE / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


def _extracted_from_frontmatter(text: str) -> Extracted | None:
    """Parse YAML frontmatter from a vault note and reconstruct Extracted."""
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    fm_text = text[3:end].strip()
    try:
        fm = yaml.safe_load(fm_text)
    except yaml.YAMLError:
        return None
    if not isinstance(fm, dict):
        return None
    return Extracted.from_dict(fm)


def _append_jira_section(note_path: Path, issues: list[dict], domain: str) -> None:
    """Append ## Jira Issues table to the vault note (idempotent)."""
    text = note_path.read_text(encoding="utf-8")
    if "## Jira Issues" in text:
        return  # already appended

    rows = ["| Issue | Summary | Kind |", "|---|---|---|"]
    for issue in issues:
        key = issue.get("key") or ""
        url = issue.get("url") or f"https://{domain}/browse/{key}"
        summary = (issue.get("summary") or "")[:80]
        kind = issue.get("kind") or ""
        rows.append(f"| [{key}]({url}) | {summary} | {kind} |")

    section = "\n## Jira Issues\n" + "\n".join(rows) + "\n"
    note_path.write_text(text + section, encoding="utf-8")


def main() -> int:
    _load_env()

    ap = argparse.ArgumentParser(description="Push meeting notes to Jira.")
    ap.add_argument("--dry-run", action="store_true", help="Preview only, no API calls")
    ap.add_argument("--message-id", help="Push a specific message ID only")
    ap.add_argument("--since", help="Only push notes on or after this date (YYYY-MM-DD)")
    ap.add_argument("--force", action="store_true", help="Re-push already-pushed notes")
    ap.add_argument("--create-decisions", action="store_true", help="Also create issues for decisions")
    args = ap.parse_args()

    # Validate required Jira env vars
    if not os.getenv("JIRA_ENABLED", "false").lower() == "true":
        if not args.dry_run:
            # Allow dry-run without JIRA_ENABLED — useful for testing
            pass
    project_key = os.getenv("JIRA_PROJECT_KEY", "")
    if not project_key:
        print("Error: JIRA_PROJECT_KEY not set in .env", file=sys.stderr)
        return 2
    issue_type = os.getenv("JIRA_ISSUE_TYPE", "Task")
    board_id = os.getenv("JIRA_BOARD_ID")

    state = State(STATE_FILE)

    # Build candidate list
    if args.message_id:
        processed = state._data["processed"]
        if args.message_id not in processed:
            print(f"Error: message_id {args.message_id!r} not found in state.json", file=sys.stderr)
            return 2
        candidates = [(args.message_id, processed[args.message_id])]
    elif args.force:
        candidates = list(state._data["processed"].items())
    else:
        candidates = state.get_unpushed()

    # Date filter
    if args.since:
        try:
            since_date = args.since.strip()
            candidates = [
                (mid, entry)
                for mid, entry in candidates
                if (entry.get("note") and _note_date(entry["note"]) >= since_date)
            ]
        except Exception as exc:
            print(f"Error applying --since filter: {exc}", file=sys.stderr)
            return 2

    if not candidates:
        print("No notes to push.")
        return 0

    # Validate Jira credentials (fast-fail before loop)
    if not args.dry_run:
        try:
            client = JiraClient()
        except JiraError as exc:
            print(f"Error initializing Jira client: {exc}", file=sys.stderr)
            return 2
        try:
            me = client.get_myself()
            logger.info("Authenticated as %s", me.get("displayName", "?"))
        except JiraError as exc:
            print(f"Jira auth failed: {exc}", file=sys.stderr)
            return 2
        try:
            proj = client.get_project(project_key)
            logger.info("Project: %s (%s)", proj.get("name", "?"), project_key)
        except JiraError as exc:
            print(f"Jira project {project_key!r} not found: {exc}", file=sys.stderr)
            return 2
    else:
        client = None  # type: ignore[assignment]

    pushed = 0
    skipped = 0

    for message_id, entry in candidates:
        note_path_str = entry.get("note", "")
        note_path = Path(note_path_str) if note_path_str else None

        if not note_path or not note_path.exists():
            logger.warning("Note file not found, skipping: %s", note_path_str)
            skipped += 1
            continue

        text = note_path.read_text(encoding="utf-8")
        ex = _extracted_from_frontmatter(text)
        if ex is None:
            logger.warning("Could not parse frontmatter in %s — skipping", note_path)
            skipped += 1
            continue

        if not ex.action_items and not args.create_decisions:
            logger.info("No action items in %s — skipping", note_path.name)
            skipped += 1
            continue

        print(f"\nProcessing: {note_path.name}")

        try:
            issues = push_meeting(
                client,  # type: ignore[arg-type]
                ex,
                str(note_path),
                message_id,
                project_key=project_key,
                issue_type=issue_type,
                create_decisions=args.create_decisions,
                dry_run=args.dry_run,
                board_id=board_id,
            )
        except JiraError as exc:
            logger.error("Jira error for %s: %s", message_id, exc)
            skipped += 1
            continue
        except Exception as exc:
            logger.error("Unexpected error for %s: %s", message_id, exc)
            skipped += 1
            continue

        if not args.dry_run and issues:
            pushed_at = datetime.now(tz=timezone.utc).isoformat()
            state.mark_jira(message_id, issues, pushed_at)
            state.save()
            domain = client.domain  # type: ignore[union-attr]
            _append_jira_section(note_path, issues, domain)
            pushed += 1
            for issue in issues:
                print(f"  Created: {issue['key']} — {issue['summary'][:60]}")
        elif args.dry_run:
            pushed += 1  # count as "would push"

    print(f"\nDone. Pushed: {pushed}, Skipped: {skipped}")
    return 0


def _note_date(note_path_str: str) -> str:
    """Extract YYYY-MM-DD prefix from note filename, or empty string."""
    name = Path(note_path_str).stem
    if len(name) >= 10 and name[:10].replace("-", "").isdigit():
        return name[:10]
    return ""


if __name__ == "__main__":
    sys.exit(main())
