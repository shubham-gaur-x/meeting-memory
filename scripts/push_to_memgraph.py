#!/usr/bin/env python3
"""
Push vault meeting notes to Memgraph.

Usage:
  python scripts/push_to_memgraph.py              # push all unpushed notes
  python scripts/push_to_memgraph.py --dry-run     # preview, no DB calls
  python scripts/push_to_memgraph.py --since 2026-01-01
  python scripts/push_to_memgraph.py --force       # re-push already-pushed notes
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent.parent
load_dotenv(HERE / ".env")
sys.path.insert(0, str(HERE))

from lib.extractor import Extracted  # noqa: E402
from lib.memgraph_writer import MemgraphWriter  # noqa: E402
from lib.state import State  # noqa: E402

VAULT_PATH = Path(os.getenv("VAULT_PATH") or (HERE / "vault")).resolve()
STATE_PATH = HERE / "state.json"


def _frontmatter_to_extracted(text: str) -> Extracted | None:
    """Parse YAML frontmatter from a vault note into an Extracted instance."""
    import yaml

    if not text.startswith("---"):
        return None
    try:
        end = text.index("---", 3)
    except ValueError:
        return None
    fm = yaml.safe_load(text[3:end])
    if not isinstance(fm, dict):
        return None
    try:
        return Extracted.from_dict(fm)
    except Exception:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Push vault notes to Memgraph")
    parser.add_argument("--dry-run", action="store_true", help="Print plan, make no DB calls")
    parser.add_argument("--since", metavar="YYYY-MM-DD", help="Only push notes on or after this date")
    parser.add_argument("--force", action="store_true", help="Re-push notes already in Memgraph")
    args = parser.parse_args()

    state = State(str(STATE_PATH))
    notes = sorted((VAULT_PATH / "Meetings").glob("*.md"))

    if args.since:
        notes = [n for n in notes if n.name[:10] >= args.since]

    writer: MemgraphWriter | None = None
    if not args.dry_run:
        writer = MemgraphWriter()
        try:
            writer.ping()
            print(f"Connected to Memgraph at {os.getenv('MEMGRAPH_HOST', 'localhost')}:{os.getenv('MEMGRAPH_PORT', '7687')}")
        except Exception as exc:
            print(f"ERROR: Cannot connect to Memgraph: {exc}")
            return 1

    # Build reverse-lookup: note_path → message_id
    note_to_mid: dict[str, str] = {
        entry.get("note", ""): mid
        for mid, entry in state._data["processed"].items()
        if entry.get("note")
    }

    pushed = skipped = failed = 0

    for note_path in notes:
        message_id = note_to_mid.get(str(note_path)) or note_path.stem

        if not args.force and state.memgraph_pushed(message_id):
            skipped += 1
            continue

        text = note_path.read_text(encoding="utf-8")
        ex = _frontmatter_to_extracted(text)
        if ex is None:
            print(f"  skip {note_path.name} — no parseable frontmatter")
            skipped += 1
            continue

        if args.dry_run:
            print(f"  [dry-run] would push: {note_path.name}")
            print(f"    title={ex.title!r}, attendees={len(ex.attendees)}, "
                  f"action_items={len(ex.action_items)}, topics={ex.topics}")
            pushed += 1
            continue

        try:
            writer.write_meeting(  # type: ignore[union-attr]
                msg=None,
                ex=ex,
                note_path=str(note_path),
                message_id=message_id,
            )
            canonical_mid = note_to_mid.get(str(note_path))
            if canonical_mid:
                state.mark_memgraph(canonical_mid, datetime.now(timezone.utc).isoformat())
                state.save()
            print(f"  pushed: {note_path.name}")
            pushed += 1
        except Exception as exc:
            print(f"  FAILED {note_path.name}: {exc}")
            failed += 1

    print(f"\nDone — pushed={pushed}, skipped={skipped}, failed={failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
