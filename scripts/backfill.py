#!/usr/bin/env python3
"""One-shot 12-month backfill.

Pulls every meeting-related email from the last year (or whatever you pass
with --months), in monthly slices so any single failure only loses a month
of progress, with a sleep between calls to stay polite to Composio.

Usage:
    python scripts/backfill.py                   # 12 months
    python scripts/backfill.py --months 24       # 2 years
    python scripts/backfill.py --months 3        # last 3 months
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT))

from lib.classifier import classify  # noqa: E402
from lib.composio_gmail import ComposioError, ComposioGmail  # noqa: E402
from lib.extractor import extract  # noqa: E402
from lib.obsidian_writer import write_meeting  # noqa: E402
from lib.state import State  # noqa: E402

console = Console()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", type=int, default=12)
    ap.add_argument("--max-per-month", type=int, default=500)
    ap.add_argument("--sleep", type=float, default=0.2)
    args = ap.parse_args()

    try:
        gmail = ComposioGmail()
    except ComposioError as e:
        console.print(f"[red]{e}[/red]")
        return 2

    vault = Path(os.getenv("VAULT_PATH") or (ROOT / "vault")).resolve()
    state = State(ROOT / "state.json")

    base_query = os.getenv("GMAIL_QUERY", "").strip()
    totals = {"fetched": 0, "skipped_seen": 0, "skipped_not_meeting": 0, "written": 0, "low_conf": 0, "errors": 0}

    for month in range(args.months):
        # Slice: messages older than `month` months but newer than `month+1` months
        older = month + 1
        newer = month if month > 0 else 0
        if newer == 0:
            slice_q = f"newer_than:{older * 30}d"
        else:
            slice_q = f"newer_than:{older * 30}d older_than:{newer * 30}d"
        query = " ".join(f"({p})" for p in (base_query, slice_q) if p)

        console.rule(f"month -{month}  ({slice_q})")
        try:
            ids = gmail.search_message_ids(query=query, max_results=args.max_per_month)
        except ComposioError as e:
            console.print(f"  [red]search failed:[/red] {e}")
            totals["errors"] += 1
            continue
        console.print(f"  found {len(ids)} candidate messages")

        if not ids:
            continue

        with Progress(
            SpinnerColumn(),
            TextColumn("{task.description}"),
            TextColumn("{task.completed}/{task.total}"),
            console=console,
        ) as prog:
            t = prog.add_task("processing", total=len(ids))
            for mid in ids:
                totals["fetched"] += 1
                if state.has(mid):
                    totals["skipped_seen"] += 1
                    prog.advance(t)
                    continue
                try:
                    msg = gmail.fetch_message(mid)
                except Exception as e:
                    console.print(f"  [red]fetch {mid} failed:[/red] {e}")
                    totals["errors"] += 1
                    prog.advance(t)
                    continue
                cls = classify(msg)
                if not cls.is_meeting:
                    totals["skipped_not_meeting"] += 1
                    prog.advance(t)
                    continue
                try:
                    ex = extract(msg)
                except Exception as e:
                    console.print(f"  [yellow]extract failed:[/yellow] {e}")
                    totals["errors"] += 1
                    prog.advance(t)
                    continue
                if ex.confidence < 0.3:
                    totals["low_conf"] += 1
                    prog.advance(t)
                    continue
                path = write_meeting(vault, msg, ex)
                state.mark(mid, str(path), kind=ex.kind, confidence=ex.confidence)
                totals["written"] += 1
                prog.advance(t)
                time.sleep(args.sleep)

        # Persist state after each month so a crash doesn't waste everything
        state.set_last_run(datetime.now(timezone.utc).isoformat())
        state.save()

    console.rule("done")
    for k, v in totals.items():
        console.print(f"  {k:25s} {v}")
    console.print(f"\nVault: {vault}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
