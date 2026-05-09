#!/usr/bin/env python3
"""Long-running listener — polls Composio for new meeting-related Gmail
messages, with adaptive backoff so idle minutes are cheap.

Designed to be run under launchd (KeepAlive=true). On crash launchd will
restart it; on each tick we dedupe against state.json so nothing is
processed twice.

Usage:
    python scripts/listen.py
    python scripts/listen.py --interval 60      # min seconds between polls
    python scripts/listen.py --window 15        # look back this many minutes
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT))

from lib.classifier import classify  # noqa: E402
from lib.composio_gmail import ComposioError, ComposioGmail  # noqa: E402
from lib.extractor import extract  # noqa: E402
from lib.obsidian_writer import write_meeting  # noqa: E402
from lib.state import State  # noqa: E402


console = Console()
RUNNING = True


def _stop(signum, frame):  # type: ignore[no-untyped-def]
    global RUNNING
    RUNNING = False
    console.print("\n[bold]received signal, shutting down[/bold]")


def tick(gmail: ComposioGmail, vault: Path, state: State, window_minutes: int) -> int:
    """One poll. Returns count of newly-written meeting notes."""
    base_query = os.getenv("GMAIL_QUERY", "").strip()
    # Gmail's `newer_than:` only supports d/m/y. Use 1d minimum but always
    # dedupe against state.json so we don't re-process old messages.
    days = max(1, (window_minutes + 1439) // 1440)
    slice_q = f"newer_than:{days}d"
    query = " ".join(f"({p})" for p in (base_query, slice_q) if p)

    try:
        ids = gmail.search_message_ids(query=query, max_results=200)
    except ComposioError as e:
        console.print(f"[red]search failed:[/red] {e}")
        return 0

    new_ids = [i for i in ids if not state.has(i)]
    if not new_ids:
        return 0

    written = 0
    for mid in new_ids:
        try:
            msg = gmail.fetch_message(mid)
        except Exception as e:
            console.print(f"  [red]fetch {mid}:[/red] {e}")
            continue
        cls = classify(msg)
        if not cls.is_meeting:
            # Mark so we don't re-classify next tick. Use a sentinel path.
            state.mark(mid, "", kind="not-meeting", confidence=0.0)
            continue
        try:
            ex = extract(msg)
        except Exception as e:
            console.print(f"  [yellow]extract {mid}:[/yellow] {e}")
            continue
        if ex.confidence < 0.3:
            state.mark(mid, "", kind="low-confidence", confidence=ex.confidence)
            continue
        path = write_meeting(vault, msg, ex)
        state.mark(mid, str(path), kind=ex.kind, confidence=ex.confidence)
        console.print(
            f"[green]+[/green] {datetime.now().strftime('%H:%M:%S')} "
            f"{path.relative_to(vault.parent)}  ({ex.kind}, conf={ex.confidence:.2f})"
        )
        written += 1
    state.set_last_run(datetime.now(timezone.utc).isoformat())
    state.save()
    return written


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=60, help="min seconds between polls")
    ap.add_argument("--max-interval", type=int, default=600, help="max seconds between polls (idle backoff)")
    ap.add_argument("--window", type=int, default=15, help="look-back window in minutes")
    args = ap.parse_args()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    try:
        gmail = ComposioGmail()
    except ComposioError as e:
        console.print(f"[red]{e}[/red]")
        return 2

    vault = Path(os.getenv("VAULT_PATH") or (ROOT / "vault")).resolve()
    state = State(ROOT / "state.json")

    interval = args.interval
    console.print(
        f"[bold]listening[/bold]  vault={vault}  interval={interval}s "
        f"(idle ↑ to {args.max_interval}s)  window={args.window}m"
    )

    while RUNNING:
        started = time.time()
        try:
            n = tick(gmail, vault, state, args.window)
        except Exception as e:  # never let a transient error kill the loop
            console.print(f"[red]tick error:[/red] {e}")
            n = -1

        # Adaptive backoff: when idle, slow down; when active, speed up.
        if n > 0:
            interval = args.interval
        else:
            interval = min(args.max_interval, max(args.interval, int(interval * 1.5)))

        elapsed = time.time() - started
        sleep_for = max(1.0, interval - elapsed)
        # Sleep in 1-second chunks so signals interrupt us promptly.
        slept = 0.0
        while RUNNING and slept < sleep_for:
            time.sleep(1)
            slept += 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
