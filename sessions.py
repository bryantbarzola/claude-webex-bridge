from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from config import CLAUDE_HISTORY_FILE, CLAUDE_PROJECTS_DIR, MAX_SESSIONS_DISPLAYED

logger = logging.getLogger(__name__)


@dataclass
class SessionInfo:
    session_id: str
    project: str
    display: str
    timestamp: int
    cwd: str
    session_path: Path


def _encode_project_path(project: str) -> str:
    """Convert a project path to the Claude directory encoding (/ → -)."""
    return project.replace("/", "-")


def _find_session_file(session_id: str, project: str) -> Path | None:
    """Locate the .jsonl file for a session on disk."""
    encoded = _encode_project_path(project)
    session_file = CLAUDE_PROJECTS_DIR / encoded / f"{session_id}.jsonl"
    if session_file.exists():
        return session_file
    return None


def _extract_cwd(session_path: Path) -> str:
    """Read the session JSONL and extract the cwd from the first user message."""
    with open(session_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("type") == "user" and "cwd" in entry:
                return entry["cwd"]
    logger.warning("No cwd found in session %s, falling back to home directory", session_path.stem)
    return str(Path.home())


def list_recent_sessions(limit: int = MAX_SESSIONS_DISPLAYED) -> list[SessionInfo]:
    """Parse history.jsonl and return the most recent sessions with verified files."""
    if not CLAUDE_HISTORY_FILE.exists():
        logger.warning("History file not found: %s", CLAUDE_HISTORY_FILE)
        return []

    # Collect the latest entry per session
    sessions: dict[str, dict] = {}
    with open(CLAUDE_HISTORY_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            sid = entry.get("sessionId")
            if not sid:
                continue
            # Keep latest entry per session (later lines overwrite earlier)
            sessions[sid] = entry

    # Sort by timestamp descending, verify files exist
    sorted_entries = sorted(sessions.values(), key=lambda e: e.get("timestamp", 0), reverse=True)

    results: list[SessionInfo] = []
    for entry in sorted_entries:
        if len(results) >= limit:
            break
        sid = entry["sessionId"]
        project = entry.get("project", "")
        session_path = _find_session_file(sid, project)
        if session_path is None:
            continue
        cwd = _extract_cwd(session_path)
        display = entry.get("display", "")
        # Truncate display for readability
        if len(display) > 80:
            display = display[:77] + "..."
        results.append(SessionInfo(
            session_id=sid,
            project=project,
            display=display,
            timestamp=entry.get("timestamp", 0),
            cwd=cwd,
            session_path=session_path,
        ))

    return results


def get_session_by_id(session_id: str) -> SessionInfo | None:
    """Look up a specific session by ID. Returns None if not found on disk."""
    if not CLAUDE_HISTORY_FILE.exists():
        return None

    # Find the session in history
    target_entry = None
    with open(CLAUDE_HISTORY_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("sessionId") == session_id:
                target_entry = entry

    if target_entry is None:
        return None

    project = target_entry.get("project", "")
    session_path = _find_session_file(session_id, project)
    if session_path is None:
        return None

    cwd = _extract_cwd(session_path)
    display = target_entry.get("display", "")
    if len(display) > 80:
        display = display[:77] + "..."

    return SessionInfo(
        session_id=session_id,
        project=project,
        display=display,
        timestamp=target_entry.get("timestamp", 0),
        cwd=cwd,
        session_path=session_path,
    )
