"""Thread-to-session mapping for Claude Code CLI sessions.

Maps Webex thread IDs (parentId) to Claude Code session UUIDs.
Persisted as a JSON file so sessions survive bot restarts.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_PATH = Path.home() / ".claude" / "webex_thread_sessions.json"
TTL_SECONDS = 48 * 3600  # 48 hours


class SessionStore:
    """Persistent thread_id -> session_id mapping with TTL."""

    def __init__(self, path: Path = DEFAULT_PATH):
        self._path = Path(path)
        self._data: dict[str, dict] = {}
        self._load()

    def _load(self):
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text())
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Failed to load session store: %s", e)
                self._data = {}

    def _save(self):
        # Atomic write: serialize to a temp file in the same dir, then rename
        # over the target. os.replace is atomic on POSIX, so a crash mid-write
        # can never truncate the existing store — readers see old or new, never
        # a partial file.
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(json.dumps(self._data, indent=2))
        os.replace(tmp, self._path)

    def get(self, thread_id: str) -> str | None:
        entry = self._data.get(thread_id)
        if entry is None:
            return None
        if time.time() - entry["created"] > TTL_SECONDS:
            del self._data[thread_id]
            self._save()
            return None
        entry["last_used"] = time.time()
        self._save()
        return entry["session_id"]

    def create(self, thread_id: str, session_id: str) -> None:
        self._data[thread_id] = {
            "session_id": session_id,
            "created": time.time(),
            "last_used": time.time(),
        }
        self._save()

    def cleanup(self):
        now = time.time()
        expired = [k for k, v in self._data.items() if now - v["created"] > TTL_SECONDS]
        for k in expired:
            del self._data[k]
        if expired:
            self._save()
            logger.info("Cleaned up %d expired sessions", len(expired))
