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
