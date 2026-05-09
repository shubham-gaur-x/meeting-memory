#!/usr/bin/env python3
"""Pull meeting-related Gmail messages and write Obsidian notes.

Usage:
    python ingest.py --since "1 hour ago" --limit 100
    python ingest.py --query "subject:(meeting OR call) newer_than:7d"
    python ingest.py --sample sample_data/   # offline test against fixtures
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dateutil import parser as dateparser
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from lib.classifier import classify
from lib.composio_gmail import ComposioGmail, GmailMessage, normalize
from lib.extractor import extract
from lib.obsidian_writer import write_meeting
from lib.state import State

console = Console()
HERE = Path(__file__).resolve().parent


def humanize_since(since: str) -> str:
    """Convert "30 days ago" / "1 hour ago" / "2026-05-01" -> Gmail newer_than/after string."""
    s = since.strip().lower()
    if s.endswith("ago"):
        n, unit, _ = s.split()
        n = int(n)
        if unit.startswith("hour"):
            # Gmail doesn't natively support hours. Use newer_than:Nd with min 1.
            return f"newer_than:{max(1, n // 24)}d"
        if unit.startswith("day"):
            return f"newer_than:{n}d"
        if unit.startswith("week"):
            return f"newer_than:{n * 7}d"
        if unit.startswith("month"):
            return f"newer_than:{n * 30}d"
    # Try absolute date
    try:
        d = dateparser.parse(since)
        return f"after:{d.strftime('%Y/%m/%d')}"
    except Exception:
        return f"newer_than:7d"


def build_query(since: str | None, extra: str | None) -> str:
    base = os.getenv("GMAIL_QUERY", "").strip()
    parts = [p for p in (base, extra) if p]
    if since:
        parts.append(humanize_since(since))
    return " ".join(f"({p})" for p in parts) or "newer_than:7d"


def load_sample_messages(folder: Path) -> list[GmailMessage]:
    msgs: list[GmailMessage] = []
    for fp in sorted(folder.glob("*.json")):
        raw = json.loads(fp.read_text())
        msgs.append(normalize(raw))
    return msgs


def process(messages, vault: Path, state: State, *, dry: bool) -> dict:
    summary = {"total": 0, "skipped_seen": 0, "skipped_not_meeting": 0, "written": 0, "low_confidence": 0}
    for msg in messages:
        summary["total"] += 1
        if state.has(msg.message_id):
            summary["skipped_seen"] += 1
            continue
        cls = classify(msg)
        console.print(
            f"[dim]{msg.received_at.date()}[/dim] "
            f"[bold]{(msg.subject or '')[:80]}[/bold] "
            f"→ score={cls.score} kind={cls.kind} ({cls.reason})"
        )
        if not cls.is_meeting:
            summary["skipped_not_meeting"] += 1
            continue
        if dry:
            console.print("  [yellow]dry-run: would extract[/yellow]")
            continue
        try:
            ex = extract(msg)
        except Exception as exc:
            console.print(f"  [red]extract failed:[/red] {exc}")
            continue
        if ex.confidence < 0.3:
            summary["low_confidence"] += 1
            console.print(f"  [yellow]low confidence ({ex.confidence}); skipping[/yellow]")
            continue
        path = write_meeting(vault, msg, ex)
        state.mark(msg.message_id, str(path), kind=ex.kind, confidence=ex.confidence)
        summary["written"] += 1
        console.print(f"  [green]wrote[/green] {path.relative_to(vault.parent)}")
    return summary


def main() -> int:
    load_dotenv(HERE / ".env")

    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help='e.g. "1 hour ago", "7 days ago", or "2026-05-01"')
    ap.add_argument("--query", help="Extra Gmail query, ANDed with the env query.")
    ap.add_argument("--limit", type=int, default=int(os.getenv("MAX_PER_RUN", "200")))
    ap.add_argument("--sample", help="Folder of *.json sample messages to use instead of Gmail.")
    ap.add_argument("--dry-run", action="store_true", help="Classify only, no LLM calls or writes.")
    args = ap.parse_args()

    vault_path = Path(os.getenv("VAULT_PATH") or (HERE / "vault")).resolve()
    vault_path.mkdir(parents=True, exist_ok=True)
    state = State(HERE / "state.json")

    if args.sample:
        folder = Path(args.sample).resolve()
        console.print(f"[bold]Loading sample messages[/bold] from {folder}")
        messages = load_sample_messages(folder)
    else:
        if not os.getenv("COMPOSIO_API_KEY"):
            console.print("[red]COMPOSIO_API_KEY missing. Either set it in .env or run with --sample.[/red]")
            return 2
        gmail = ComposioGmail(entity_id=os.getenv("COMPOSIO_ENTITY_ID", "default"))
        query = build_query(args.since, args.query)
        console.print(f"[bold]Gmail query:[/bold] {query}")
        messages = gmail.iter_messages(query=query, max_results=args.limit)

    summary = process(messages, vault_path, state, dry=args.dry_run)
    state.set_last_run(datetime.now(timezone.utc).isoformat())
    state.save()

    table = Table(title="Ingest summary", show_header=False)
    for k, v in summary.items():
        table.add_row(k, str(v))
    for k, v in state.stats().items():
        table.add_row(f"state.{k}", str(v))
    console.print(table)
    return 0


if __name__ == "__main__":
    sys.exit(main())
