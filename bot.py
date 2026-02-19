from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from auth import is_authorized
from claude_cli import send_message as cli_send_message
from config import POLL_INTERVAL_SECONDS, WEBEX_MAX_MESSAGE_BYTES
from sessions import SessionInfo, get_session_by_id, list_recent_sessions
from webex_api import WebexAPI

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Suppress httpx INFO logs (too verbose during polling)
logging.getLogger("httpx").setLevel(logging.WARNING)


@dataclass
class BotState:
    session_id: str | None = None
    session_cwd: str | None = None
    session_label: str = ""
    skip_permissions: bool = False
    pending_sessions: list[SessionInfo] = field(default_factory=list)
    processing: bool = False


_room_states: dict[str, BotState] = {}


def get_state(room_id: str) -> BotState:
    """Get or create per-room bot state."""
    if room_id not in _room_states:
        _room_states[room_id] = BotState()
    return _room_states[room_id]


# ---------------------------------------------------------------------------
# Message splitting (byte-aware for Webex)
# ---------------------------------------------------------------------------

def split_message(text: str, max_bytes: int = WEBEX_MAX_MESSAGE_BYTES) -> list[str]:
    """Split text into chunks that each fit within max_bytes when UTF-8 encoded."""
    if len(text.encode("utf-8")) <= max_bytes:
        return [text]

    chunks: list[str] = []
    current = ""

    for line in text.split("\n"):
        candidate = current + "\n" + line if current else line
        if len(candidate.encode("utf-8")) > max_bytes:
            if current:
                chunks.append(current)
                current = ""
            # Check if the single line itself exceeds the limit
            if len(line.encode("utf-8")) > max_bytes:
                # Hard-split the line without breaking multi-byte characters
                chunks.extend(_hard_split_line(line, max_bytes))
            else:
                current = line
        else:
            current = candidate

    if current:
        chunks.append(current)
    return chunks


def _hard_split_line(line: str, max_bytes: int) -> list[str]:
    """Split a single long line by byte length without breaking UTF-8 characters."""
    parts: list[str] = []
    current = ""
    for char in line:
        candidate = current + char
        if len(candidate.encode("utf-8")) > max_bytes:
            parts.append(current)
            current = char
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Session displays that aren't useful to show
_SKIP_DISPLAYS = {"/exit", "/help", "/start", "/resume", "/sessions", ""}


def _relative_time(epoch_ms: int) -> str:
    """Convert an epoch-millisecond timestamp to a human-readable relative time."""
    now = time.time() * 1000
    diff_seconds = (now - epoch_ms) / 1000
    if diff_seconds < 0:
        return "just now"
    if diff_seconds < 60:
        return "just now"
    minutes = int(diff_seconds / 60)
    if minutes < 60:
        return f"{minutes}m ago"
    hours = int(minutes / 60)
    if hours < 24:
        return f"{hours}h ago"
    days = int(hours / 24)
    if days < 30:
        return f"{days}d ago"
    return f"{int(days / 30)}mo ago"


def _short_path(cwd: str) -> str:
    """Shorten a working directory path for display."""
    parts = PurePosixPath(cwd).parts
    home_parts = PurePosixPath.home().parts if hasattr(PurePosixPath, 'home') else ()
    # Try to make it relative to home
    try:
        from pathlib import Path
        home = str(Path.home())
        if cwd.startswith(home):
            relative = cwd[len(home):]
            if relative.startswith("/"):
                relative = relative[1:]
            return f"~/{relative}" if relative else "~"
    except Exception:
        pass
    # Fallback: show last 2 directory components
    if len(parts) > 2:
        return f".../{'/'.join(parts[-2:])}"
    return cwd


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def handle_start(api: WebexAPI, room_id: str) -> None:
    await api.send_message(
        room_id,
        "**Claude Code Bridge (Webex)**\n\n"
        "Commands:\n"
        "- `/sessions` - List recent sessions\n"
        "- `/connect N` - Connect to session N from the list\n"
        "- `/disconnect` - Disconnect from session\n"
        "- `/status` - Show connection status\n"
        "- `/safe` - Toggle permission mode\n\n"
        "Connect to a session, then send messages to interact with Claude Code.",
    )


def _build_sessions_card(sessions: list[SessionInfo]) -> dict:
    """Build an Adaptive Card JSON for the session list."""
    body: list[dict] = [
        {
            "type": "TextBlock",
            "text": "Recent Sessions",
            "size": "Medium",
            "weight": "Bolder",
        }
    ]

    for i, s in enumerate(sessions, 1):
        label = s.display if s.display else s.session_id[:12]
        path = _short_path(s.cwd)
        ago = _relative_time(s.timestamp)

        body.append(
            {
                "type": "Container",
                "separator": True,
                "items": [
                    {
                        "type": "ColumnSet",
                        "columns": [
                            {
                                "type": "Column",
                                "width": "auto",
                                "items": [
                                    {
                                        "type": "TextBlock",
                                        "text": str(i),
                                        "weight": "Bolder",
                                    }
                                ],
                            },
                            {
                                "type": "Column",
                                "width": "stretch",
                                "items": [
                                    {
                                        "type": "TextBlock",
                                        "text": label,
                                        "wrap": True,
                                    },
                                    {
                                        "type": "TextBlock",
                                        "text": f"{path} \u00b7 {ago}",
                                        "size": "Small",
                                        "isSubtle": True,
                                        "spacing": "None",
                                    },
                                ],
                            },
                        ],
                    }
                ],
            }
        )

    body.append(
        {
            "type": "Container",
            "separator": True,
            "style": "accent",
            "items": [
                {
                    "type": "TextBlock",
                    "text": "Reply with `/connect N` to connect to a session",
                    "weight": "Bolder",
                    "wrap": True,
                }
            ],
        }
    )

    return {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.2",
        "body": body,
    }


async def handle_sessions(api: WebexAPI, room_id: str) -> None:
    state = get_state(room_id)
    # Fetch extra sessions to account for filtered ones
    all_sessions = list_recent_sessions(limit=20)
    if not all_sessions:
        await api.send_message(room_id, "No recent sessions found.")
        return

    # Filter out sessions with useless display names
    filtered = [s for s in all_sessions if s.display.strip().lower() not in _SKIP_DISPLAYS]

    # Fall back to unfiltered if everything got filtered
    if not filtered:
        filtered = all_sessions

    # Limit to 5 for display
    filtered = filtered[:5]
    state.pending_sessions = filtered

    # Build fallback text for clients without card support
    lines = ["Recent Sessions\n"]
    for i, s in enumerate(filtered, 1):
        label = s.display if s.display else s.session_id[:12]
        path = _short_path(s.cwd)
        ago = _relative_time(s.timestamp)
        lines.append(f"{i}. {label}")
        lines.append(f"   {path} \u00b7 {ago}\n")
    lines.append("Use /connect N to connect to a session.")
    fallback_text = "\n".join(lines)

    card = _build_sessions_card(filtered)
    await api.send_card_message(room_id, card, fallback_text)


async def handle_connect(api: WebexAPI, room_id: str, arg: str) -> None:
    state = get_state(room_id)
    if not arg:
        await api.send_message(room_id, "Usage: `/connect N` (run `/sessions` first)")
        return

    try:
        index = int(arg)
    except ValueError:
        await api.send_message(room_id, "Invalid number. Usage: `/connect N`")
        return

    if not state.pending_sessions:
        await api.send_message(room_id, "No session list cached. Run `/sessions` first.")
        return

    if index < 1 or index > len(state.pending_sessions):
        await api.send_message(
            room_id,
            f"Out of range. Pick a number between 1 and {len(state.pending_sessions)}.",
        )
        return

    selected = state.pending_sessions[index - 1]

    # Re-verify the session still exists on disk
    session = get_session_by_id(selected.session_id)
    if session is None:
        await api.send_message(room_id, "Session not found. It may have been deleted. Run `/sessions` again.")
        return

    state.session_id = session.session_id
    state.session_cwd = session.cwd
    state.session_label = session.display or session.session_id[:12]

    mode = "skip-permissions" if state.skip_permissions else "safe"
    await api.send_message(
        room_id,
        f"**Connected to:** {state.session_label}\n"
        f"**Project:** {session.project}\n"
        f"**Working dir:** {session.cwd}\n"
        f"**Mode:** {mode}\n\n"
        "Send a message to interact with this session.",
    )


async def handle_disconnect(api: WebexAPI, room_id: str) -> None:
    state = get_state(room_id)
    if state.session_id is None:
        await api.send_message(room_id, "Not connected to any session.")
        return

    label = state.session_label
    state.session_id = None
    state.session_cwd = None
    state.session_label = ""
    await api.send_message(room_id, f"Disconnected from: {label}")


async def handle_status(api: WebexAPI, room_id: str) -> None:
    state = get_state(room_id)
    if state.session_id is None:
        connected = "Not connected"
    else:
        connected = (
            f"**Connected to:** {state.session_label}\n"
            f"**Session ID:** {state.session_id}\n"
            f"**Working dir:** {state.session_cwd}"
        )

    mode = "skip-permissions" if state.skip_permissions else "safe"
    await api.send_message(room_id, f"{connected}\n**Mode:** {mode}")


async def handle_safe(api: WebexAPI, room_id: str) -> None:
    state = get_state(room_id)
    state.skip_permissions = not state.skip_permissions

    if state.skip_permissions:
        await api.send_message(
            room_id,
            "**Mode: skip-permissions**\n"
            "Claude will execute tools without asking for approval.",
        )
    else:
        await api.send_message(
            room_id,
            "**Mode: safe**\n"
            "WARNING: In --print mode, Claude cannot prompt for interactive permission "
            "approval. Commands requiring approval may cause the CLI to hang. "
            "Use `/safe` again to switch back if this happens.",
        )


async def handle_text_message(api: WebexAPI, room_id: str, text: str) -> None:
    """Forward a plain text message to the connected Claude session."""
    state = get_state(room_id)
    if state.session_id is None:
        await api.send_message(room_id, "Not connected. Use `/sessions` to pick a session.")
        return

    if state.processing:
        await api.send_message(room_id, "Still processing the previous message. Please wait.")
        return

    state.processing = True
    thinking_id = None
    try:
        # Send "Thinking..." placeholder
        thinking = await api.send_message(room_id, "Thinking...")
        thinking_id = thinking.get("id")

        response = await cli_send_message(
            session_id=state.session_id,
            message=text,
            cwd=state.session_cwd,
            skip_permissions=state.skip_permissions,
        )

        chunks = split_message(response)

        # Edit "Thinking..." with first chunk, fallback to new message
        if thinking_id:
            result = await api.edit_message(thinking_id, room_id, chunks[0])
            if result is None:
                await api.send_message(room_id, chunks[0])
        else:
            await api.send_message(room_id, chunks[0])

        # Send remaining chunks as new messages
        for chunk in chunks[1:]:
            await api.send_message(room_id, chunk)
    except Exception:
        logger.exception("Error processing message")
        error_text = "An error occurred while processing your message. Check the bot logs for details."
        if thinking_id:
            await api.edit_message(thinking_id, room_id, error_text)
        else:
            await api.send_message(room_id, error_text)
    finally:
        state.processing = False


# ---------------------------------------------------------------------------
# Command dispatch
# ---------------------------------------------------------------------------

COMMANDS = {
    "/start": handle_start,
    "/help": handle_start,
    "/sessions": handle_sessions,
    "/disconnect": handle_disconnect,
    "/status": handle_status,
    "/safe": handle_safe,
}


async def dispatch(api: WebexAPI, room_id: str, text: str) -> None:
    """Route an incoming message to the appropriate handler."""
    stripped = text.strip()

    if not stripped.startswith("/"):
        await handle_text_message(api, room_id, stripped)
        return

    parts = stripped.split(None, 1)
    command = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ""

    if command == "/connect":
        await handle_connect(api, room_id, arg.strip())
    elif command in COMMANDS:
        await COMMANDS[command](api, room_id)
    else:
        await api.send_message(room_id, f"Unknown command: `{command}`\nUse `/help` for available commands.")


# ---------------------------------------------------------------------------
# Polling loop
# ---------------------------------------------------------------------------

async def poll_loop(api: WebexAPI) -> None:
    """Poll Webex for new messages in direct rooms."""
    # Track the newest seen message ID per room to avoid replaying history
    last_seen: dict[str, str] = {}
    # Rooms we've initialized (first poll marks position, doesn't process)
    initialized_rooms: set[str] = set()

    logger.info("Polling started (interval=%.1fs)", POLL_INTERVAL_SECONDS)

    while True:
        try:
            rooms = await api.list_direct_rooms(max_rooms=50)

            for room in rooms:
                room_id = room["id"]
                messages = await api.list_messages(room_id, max_messages=10)

                if not messages:
                    continue

                newest_id = messages[0]["id"]

                # First time seeing this room: mark position, skip processing
                if room_id not in initialized_rooms:
                    initialized_rooms.add(room_id)
                    last_seen[room_id] = newest_id
                    logger.info("Initialized room %s (last_seen=%s)", room_id[:12], newest_id[:12])
                    continue

                # No new messages
                if last_seen.get(room_id) == newest_id:
                    continue

                # Collect messages newer than last-seen
                new_messages = []
                for msg in messages:
                    if msg["id"] == last_seen.get(room_id):
                        break
                    new_messages.append(msg)

                # Update position
                last_seen[room_id] = newest_id

                # Process in chronological order (API returns newest-first)
                new_messages.reverse()

                for msg in new_messages:
                    # Skip bot's own messages
                    if msg.get("personId") == api.bot_id:
                        continue

                    # Check authorization
                    sender_email = msg.get("personEmail", "")
                    if not is_authorized(sender_email):
                        continue

                    text = msg.get("text", "").strip()
                    if not text:
                        continue

                    logger.info("Message from %s: %s", sender_email, text[:80])
                    await dispatch(api, room_id, text)

        except SystemExit:
            raise
        except Exception:
            logger.exception("Error during poll cycle")
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

        await asyncio.sleep(POLL_INTERVAL_SECONDS)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def async_main() -> None:
    api = WebexAPI()
    await api.start()
    try:
        await poll_loop(api)
    finally:
        await api.close()


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
