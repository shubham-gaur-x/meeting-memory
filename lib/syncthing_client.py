"""Trigger Syncthing folder rescan after vault writes."""
from __future__ import annotations

import os

import httpx


class SyncthingClient:
    def __init__(
        self,
        url: str | None = None,
        api_key: str | None = None,
        folder_id: str | None = None,
    ):
        self._url = (url or os.getenv("SYNCTHING_URL", "http://127.0.0.1:8384")).rstrip("/")
        self._api_key = api_key or os.getenv("SYNCTHING_API_KEY", "")
        self._folder_id = folder_id or os.getenv("SYNCTHING_FOLDER_ID", "vault")

    def _headers(self) -> dict[str, str]:
        return {"X-API-Key": self._api_key}

    def ping(self) -> bool:
        """Return True if Syncthing is reachable and the API key is valid."""
        try:
            r = httpx.get(
                f"{self._url}/rest/system/ping",
                headers=self._headers(),
                timeout=5.0,
            )
            return r.status_code == 200 and r.json().get("ping") == "pong"
        except Exception:
            return False

    def rescan(self) -> bool:
        """
        Trigger immediate rescan of the configured vault folder.
        Returns True on success. Never raises — treated as fire-and-forget.
        """
        try:
            r = httpx.post(
                f"{self._url}/rest/db/scan",
                params={"folder": self._folder_id},
                headers=self._headers(),
                timeout=5.0,
            )
            return r.status_code == 200
        except Exception:
            return False

    def status(self) -> dict:
        """Return folder sync status dict from Syncthing API."""
        r = httpx.get(
            f"{self._url}/rest/db/status",
            params={"folder": self._folder_id},
            headers=self._headers(),
            timeout=10.0,
        )
        r.raise_for_status()
        return r.json()
