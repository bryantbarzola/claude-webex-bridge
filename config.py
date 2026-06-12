import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


# Known Claude permission modes for spaces. "safe" is intentionally excluded
# (its approval-card UX is not built) and downgrades to "strict".
_SPACE_VALID_MODES = {"yolo", "strict"}


def _parse_space_modes(raw: str) -> dict[str, str]:
    """Parse 'roomId:mode,roomId:mode' into {room_id: mode}.

    Unknown modes (incl. 'safe') downgrade to 'strict'. Malformed entries
    (not exactly one colon) are skipped. Empty/blank -> {}.
    """
    result: dict[str, str] = {}
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split(":")
        if len(parts) != 2:
            continue
        room_id, mode = parts[0].strip(), parts[1].strip().lower()
        if not room_id or not mode:
            continue
        if mode not in _SPACE_VALID_MODES:
            print(f"Warning: SPACE_MODES mode {mode!r} for {room_id!r} is not allowed; using 'strict'.", file=sys.stderr)
            mode = "strict"
        result[room_id] = mode
    return result


def _require_env(name: str) -> str:
    """Read a required environment variable, exiting with a helpful message if missing or empty."""
    value = os.environ.get(name, "").strip()
    if not value:
        print(f"Error: {name} is not set. Copy .env.example to .env and fill in your values.", file=sys.stderr)
        sys.exit(1)
    return value


WEBEX_BOT_TOKEN: str = _require_env("WEBEX_BOT_TOKEN")
WEBEX_USER_EMAIL: str = _require_env("WEBEX_USER_EMAIL")

BOT_DISPLAY_NAME: str = os.environ.get("BOT_DISPLAY_NAME", "Claude Code Bridge").strip()
BOT_TAGLINE: str = os.environ.get("BOT_TAGLINE", "").strip()

WEBEX_BASE_URL: str = "https://webexapis.com/v1"
WEBEX_MAX_MESSAGE_BYTES: int = 7000
POLL_INTERVAL_SECONDS: float = 2.5

CLAUDE_HISTORY_FILE: Path = Path.home() / ".claude" / "history.jsonl"
CLAUDE_PROJECTS_DIR: Path = Path.home() / ".claude" / "projects"
MAX_SESSIONS_DISPLAYED: int = 10
def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"Warning: {name}={raw!r} is not a valid integer, using default ({default})", file=sys.stderr)
        return default


CLI_TIMEOUT_SECONDS: int = _int_env("CLI_TIMEOUT_SECONDS", 2400)
CLI_IDLE_TIMEOUT_SECONDS: int = _int_env("CLI_IDLE_TIMEOUT_SECONDS", 180)

# room_id -> permission mode for group spaces. Unlisted spaces default to strict
# (read-only) at the call site. See agent-platform-space-perms-spec.
SPACE_MODES: dict[str, str] = _parse_space_modes(os.environ.get("SPACE_MODES", ""))
