"""Tiny JSON-on-disk store of processed message IDs."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path


class State:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._data: dict = {"processed": {}, "last_run": None}
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text() or "{}")
                self._data.setdefault("processed", {})
            except json.JSONDecodeError:
                pass

    def has(self, message_id: str) -> bool:
        return message_id in self._data["processed"]

    def mark(self, message_id: str, note_path: str, *, kind: str, confidence: float) -> None:
        with self._lock:
            self._data["processed"][message_id] = {
                "note": note_path,
                "kind": kind,
                "confidence": confidence,
            }

    def set_last_run(self, iso_ts: str) -> None:
        self._data["last_run"] = iso_ts

    def save(self) -> None:
        with self._lock:
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(self._data, indent=2, sort_keys=True))
            os.replace(tmp, self.path)

    def stats(self) -> dict:
        return {"processed_total": len(self._data["processed"]), "last_run": self._data.get("last_run")}

    def mark_jira(self, message_id: str, issues: list[dict], pushed_at: str) -> None:
        """Attach jira_issues list to existing processed entry. Replaces on re-push."""
        with self._lock:
            entry = self._data["processed"].get(message_id)
            if entry is None:
                return
            entry["jira_issues"] = issues
            entry["jira_pushed_at"] = pushed_at

    def jira_pushed(self, message_id: str) -> bool:
        """True if jira_issues is a non-empty list for this message_id."""
        entry = self._data["processed"].get(message_id)
        if entry is None:
            return False
        issues = entry.get("jira_issues")
        return isinstance(issues, list) and len(issues) > 0

    def get_unpushed(self) -> list[tuple[str, dict]]:
        """Entries with a note path but no jira_issues. For push_to_jira.py backfill."""
        result = []
        for message_id, entry in self._data["processed"].items():
            if entry.get("note") and not self.jira_pushed(message_id):
                result.append((message_id, entry))
        return result
