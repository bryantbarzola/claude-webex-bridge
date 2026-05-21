import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


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
CLI_TIMEOUT_SECONDS: int = int(os.environ.get("CLI_TIMEOUT_SECONDS", "2400"))
CLI_IDLE_TIMEOUT_SECONDS: int = int(os.environ.get("CLI_IDLE_TIMEOUT_SECONDS", "180"))
