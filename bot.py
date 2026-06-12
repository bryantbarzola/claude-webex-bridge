from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from auth import is_authorized
from claude_cli import (
    PermissionMode,
    StreamEvent,
    ToolUseEvent,
    generate_session_id,
    send_message as cli_send_message,
)
from config import BOT_DISPLAY_NAME, BOT_TAGLINE, POLL_INTERVAL_SECONDS, WEBEX_MAX_MESSAGE_BYTES, WEBEX_USER_EMAIL
from mentions import strip_mention, thread_id_of
from session_store import SessionStore
from sessions import SessionInfo, get_session_by_id, list_recent_sessions
from webex_api import WebexAPI

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

logging.getLogger("httpx").setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

@dataclass
class BotState:
    session_id: str | None = None
    session_cwd: str | None = None
    session_label: str = ""
    session_is_new: bool = False
    mode: str = PermissionMode.YOLO
    pending_sessions: list[SessionInfo] = field(default_factory=list)
    processing: bool = False
    _active_process: asyncio.subprocess.Process | None = field(default=None, repr=False)
    _thinking_id: str | None = field(default=None, repr=False)
    _last_tool: str = field(default="", repr=False)


_room_states: dict[str, BotState] = {}

# Disk-backed thread_id -> claude session_id map for group-space conversations.
_thread_sessions = SessionStore()


def get_state(room_id: str) -> BotState:
    if room_id not in _room_states:
        _room_states[room_id] = BotState()
    return _room_states[room_id]


def _cleanup_expired_sessions() -> None:
    """Evict expired thread sessions from disk AND their in-memory BotState,
    so _room_states does not grow without bound on a long-running bot."""
    for thread in _thread_sessions.cleanup():
        _room_states.pop(thread, None)


async def handle_space_mention(api: WebexAPI, room_id: str, message: dict) -> None:
    """Handle one @mention in a group space: resolve thread, resume/create its
    Claude session, and reply in-thread. State is keyed by thread id so each
    Webex thread has its own conversation context."""
    thread = thread_id_of(message)
    question = strip_mention(message.get("text", ""), message.get("html", ""), api.bot_display_name)
    if not question:
        return

    state = get_state(thread)
    existing = _thread_sessions.get(thread)
    if existing:
        state.session_id = existing
        state.session_is_new = False
    elif state.session_id is None:
        state.session_id = generate_session_id()
        state.session_is_new = True
        state.session_cwd = str(Path.home())
        _thread_sessions.create(thread, state.session_id)

    logger.info("Space mention thread=%s: %s", thread[:12], question[:80])
    await handle_text_message(api, room_id, question, state_key=thread, parent_id=thread)


# ---------------------------------------------------------------------------
# Message splitting
# ---------------------------------------------------------------------------

def split_message(text: str, max_bytes: int = WEBEX_MAX_MESSAGE_BYTES) -> list[str]:
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
            if len(line.encode("utf-8")) > max_bytes:
                chunks.extend(_hard_split_line(line, max_bytes))
            else:
                current = line
        else:
            current = candidate

    if current:
        chunks.append(current)
    return chunks


def _hard_split_line(line: str, max_bytes: int) -> list[str]:
    parts: list[str] = []
    current = ""
    for char in line:
        candidate = current + char
        if len(candidate.encode("utf-8")) > max_bytes:
            if current:
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

_SKIP_DISPLAYS = {"/exit", "/help", "/resume", "/sessions", ""}


def _relative_time(epoch_ms: int) -> str:
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
    try:
        home = str(Path.home())
        if cwd.startswith(home):
            relative = cwd[len(home):]
            if relative.startswith("/") or relative.startswith("\\"):
                relative = relative[1:]
            return f"~/{relative}" if relative else "~"
    except Exception:
        pass
    parts = Path(cwd).parts
    if len(parts) > 2:
        return f".../{'/'.join(parts[-2:])}"
    return cwd


def _format_elapsed(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m = s // 60
    remaining = s % 60
    if remaining == 0:
        return f"{m}m"
    return f"{m}m {remaining}s"


def _mode_label(mode: str) -> str:
    if mode == PermissionMode.YOLO:
        return "yolo (auto-approve all)"
    elif mode == PermissionMode.SAFE:
        return "safe (asks before tools)"
    elif mode == PermissionMode.STRICT:
        return "strict (read-only tools)"
    return mode


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

def _build_help_card(state: BotState | None = None) -> tuple[dict, str]:
    if state and state.session_id:
        path = _short_path(state.session_cwd) if state.session_cwd else "~"
        status_text = f"● Active · {path} · {state.mode}"
    else:
        status_text = "○ No active session"

    card = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.2",
        "body": [
            {
                "type": "Container",
                "style": "accent",
                "items": [
                    {
                        "type": "TextBlock",
                        "text": BOT_DISPLAY_NAME,
                        "size": "large",
                        "weight": "bolder",
                    },
                    {
                        "type": "TextBlock",
                        "text": status_text,
                        "size": "small",
                        "spacing": "small",
                    },
                ],
            },
            {
                "type": "TextBlock",
                "text": "Just type a message to chat. Commands:",
                "wrap": True,
                "spacing": "medium",
                "size": "small",
            },
            {
                "type": "FactSet",
                "facts": [
                    {"title": "/new [dir]", "value": "Start a fresh session"},
                    {"title": "/sessions", "value": "List recent sessions"},
                    {"title": "/resume N", "value": "Resume session N"},
                    {"title": "/status", "value": "Current session info"},
                    {"title": "/cancel", "value": "Cancel running task"},
                    {"title": "/disconnect", "value": "Disconnect from session"},
                    {"title": "/yolo", "value": "Auto-approve all tools"},
                    {"title": "/safe", "value": "Ask before tool use"},
                    {"title": "/strict", "value": "Read-only tools only"},
                ],
            },
        ],
    }

    fallback = (
        f"{BOT_DISPLAY_NAME} — {status_text}\n\n"
        "Commands: /new, /sessions, /resume N, /status, /cancel, /disconnect, /yolo, /safe, /strict"
    )
    return card, fallback


async def handle_help(api: WebexAPI, room_id: str) -> None:
    state = get_state(room_id)
    card, fallback = _build_help_card(state)
    await api.send_card_message(room_id, card, fallback)


def _build_sessions_card(sessions: list[SessionInfo]) -> dict:
    body: list[dict] = [
        {
            "type": "TextBlock",
            "text": "Recent Sessions",
            "size": "Medium",
            "weight": "Bolder",
        }
    ]

    for i, s in enumerate(sessions, 1):
        display = s.display if s.display else s.session_id[:12]
        ago = _relative_time(s.timestamp)
        if len(display) > 60:
            display = display[:57] + "..."

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
                                "verticalContentAlignment": "Center",
                                "items": [
                                    {
                                        "type": "TextBlock",
                                        "text": str(i),
                                        "size": "Large",
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
                                        "text": display,
                                        "weight": "Bolder",
                                        "wrap": True,
                                    },
                                    {
                                        "type": "TextBlock",
                                        "text": f"{s.session_id[:8]}... · {ago}",
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
                    "text": "Reply `/resume N` to connect (e.g. `/resume 1`)",
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
    all_sessions = await asyncio.to_thread(list_recent_sessions, 20)
    if not all_sessions:
        await api.send_message(room_id, "No recent sessions found.")
        return

    filtered = [s for s in all_sessions if s.display.strip().lower() not in _SKIP_DISPLAYS]
    if not filtered:
        filtered = all_sessions
    filtered = filtered[:5]
    state.pending_sessions = filtered

    lines = ["**Recent Sessions**\n"]
    for i, s in enumerate(filtered, 1):
        display = s.display if s.display else s.session_id[:12]
        if len(display) > 60:
            display = display[:57] + "..."
        ago = _relative_time(s.timestamp)
        lines.append(f"**{i}.** {display}")
        lines.append(f"   {s.session_id[:8]}... · {ago}\n")
    lines.append("Reply `/resume N` to connect")
    fallback_text = "\n".join(lines)

    card = _build_sessions_card(filtered)
    await api.send_card_message(room_id, card, fallback_text)


async def _connect_to_session(api: WebexAPI, room_id: str, session: SessionInfo) -> None:
    state = get_state(room_id)
    state.session_id = session.session_id
    state.session_cwd = session.cwd
    state.session_label = session.display or session.session_id[:12]
    state.session_is_new = False

    path = _short_path(session.cwd)

    card = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.2",
        "body": [
            {
                "type": "TextBlock",
                "text": "Connected",
                "size": "Medium",
                "weight": "Bolder",
            },
            {
                "type": "FactSet",
                "facts": [
                    {"title": "Session", "value": state.session_label},
                    {"title": "Directory", "value": path},
                    {"title": "Mode", "value": _mode_label(state.mode)},
                ],
            },
            {
                "type": "TextBlock",
                "text": "Send a message to continue.",
                "isSubtle": True,
                "spacing": "Medium",
            },
        ],
    }
    fallback = f"Connected to: {state.session_label}\nDirectory: {path}\nMode: {_mode_label(state.mode)}"
    await api.send_card_message(room_id, card, fallback)


async def handle_new_session(api: WebexAPI, room_id: str, arg: str) -> None:
    state = get_state(room_id)

    if arg:
        target = Path(arg).expanduser().resolve()
        if not target.is_dir():
            await api.send_message(room_id, f"Directory not found: `{arg}`")
            return
        cwd = str(target)
    else:
        cwd = str(Path.home())

    state.session_id = generate_session_id()
    state.session_cwd = cwd
    state.session_label = "New session"
    state.session_is_new = True

    path = _short_path(cwd)
    card = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.2",
        "body": [
            {
                "type": "TextBlock",
                "text": "New Session",
                "size": "Medium",
                "weight": "Bolder",
            },
            {
                "type": "FactSet",
                "facts": [
                    {"title": "Directory", "value": path},
                    {"title": "Mode", "value": _mode_label(state.mode)},
                ],
            },
            {
                "type": "TextBlock",
                "text": "Send your first message to begin.",
                "isSubtle": True,
                "spacing": "Medium",
            },
        ],
    }
    fallback = f"New Session\nDirectory: {path}\nMode: {_mode_label(state.mode)}"
    await api.send_card_message(room_id, card, fallback)


async def handle_resume(api: WebexAPI, room_id: str, arg: str) -> None:
    state = get_state(room_id)

    if not arg:
        all_sessions = await asyncio.to_thread(list_recent_sessions, 5)
        if not all_sessions:
            await api.send_message(room_id, "No recent sessions found.")
            return
        await _connect_to_session(api, room_id, all_sessions[0])
        return

    try:
        index = int(arg)
    except ValueError:
        await api.send_message(room_id, "Invalid number. Usage: `/resume N`")
        return

    if not state.pending_sessions:
        await api.send_message(room_id, "Run `/sessions` first.")
        return

    if index < 1 or index > len(state.pending_sessions):
        await api.send_message(room_id, f"Pick between 1 and {len(state.pending_sessions)}.")
        return

    selected = state.pending_sessions[index - 1]
    session = await asyncio.to_thread(get_session_by_id, selected.session_id)
    if session is None:
        await api.send_message(room_id, "Session not found. Run `/sessions` again.")
        return

    await _connect_to_session(api, room_id, session)


async def handle_disconnect(api: WebexAPI, room_id: str) -> None:
    state = get_state(room_id)
    if state.session_id is None:
        await api.send_message(room_id, "Not connected to any session.")
        return

    label = state.session_label
    state.session_id = None
    state.session_cwd = None
    state.session_label = ""
    state.session_is_new = False
    await api.send_message(room_id, f"Disconnected from: {label}")


async def handle_status(api: WebexAPI, room_id: str) -> None:
    state = get_state(room_id)

    if state.session_id is None:
        facts = [
            {"title": "Session", "value": "Not connected"},
            {"title": "Mode", "value": _mode_label(state.mode)},
        ]
    else:
        path = _short_path(state.session_cwd)
        facts = [
            {"title": "Session", "value": state.session_label},
            {"title": "Directory", "value": path},
            {"title": "Mode", "value": _mode_label(state.mode)},
        ]

    card = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.2",
        "body": [
            {"type": "TextBlock", "text": "Status", "size": "Medium", "weight": "Bolder"},
            {"type": "FactSet", "facts": facts},
        ],
    }
    fallback = "\n".join(f"{f['title']}: {f['value']}" for f in facts)
    await api.send_card_message(room_id, card, fallback)


async def handle_mode(api: WebexAPI, room_id: str, mode: str) -> None:
    state = get_state(room_id)
    state.mode = mode
    await api.send_message(room_id, f"Mode: **{_mode_label(mode)}**")


async def handle_cancel(api: WebexAPI, room_id: str) -> None:
    state = get_state(room_id)
    if not state.processing:
        await api.send_message(room_id, "Nothing to cancel.")
        return

    process = state._active_process
    if process is not None:
        try:
            process.kill()
            await asyncio.wait_for(process.wait(), timeout=5.0)
        except (ProcessLookupError, asyncio.TimeoutError):
            pass

    if state._thinking_id:
        await api.edit_message(state._thinking_id, room_id, "Cancelled.")

    state._active_process = None
    state._thinking_id = None
    state.processing = False
    logger.info("Cancelled in room %s", room_id[:12])


# ---------------------------------------------------------------------------
# Thinking indicator with tool visibility
# ---------------------------------------------------------------------------

async def _update_thinking(api: WebexAPI, state: BotState, room_id: str, tool_event: asyncio.Event) -> None:
    start = time.monotonic()
    try:
        while True:
            # Wait for either a tool event or 15s timeout (for elapsed time updates)
            try:
                await asyncio.wait_for(tool_event.wait(), timeout=15)
                tool_event.clear()
            except asyncio.TimeoutError:
                pass

            elapsed = _format_elapsed(time.monotonic() - start)
            tool_info = f" · {state._last_tool}" if state._last_tool else ""
            await api.edit_message(state._thinking_id, room_id, f"Thinking... ({elapsed}{tool_info})")
    except asyncio.CancelledError:
        pass


# ---------------------------------------------------------------------------
# Text message handler (core loop)
# ---------------------------------------------------------------------------

async def handle_text_message(
    api: WebexAPI, room_id: str, text: str,
    state_key: str | None = None, parent_id: str | None = None,
) -> None:
    state = get_state(state_key or room_id)

    if state.session_id is None:
        state.session_id = generate_session_id()
        state.session_is_new = True
        state.session_cwd = str(Path.home())

    if state.processing:
        await api.send_message(room_id, "Still processing. Use `/cancel` to abort.", parent_id=parent_id)
        return

    state.processing = True
    state._last_tool = ""
    thinking_id = None
    updater_task = None
    tool_event = asyncio.Event()

    try:
        thinking = await api.send_message(room_id, "Thinking...", parent_id=parent_id)
        thinking_id = thinking.get("id")
        state._thinking_id = thinking_id

        if thinking_id:
            updater_task = asyncio.create_task(_update_thinking(api, state, room_id, tool_event))

        def on_event(event: StreamEvent) -> None:
            if isinstance(event, ToolUseEvent):
                state._last_tool = event.tool_name
                tool_event.set()

        response = await cli_send_message(
            session_id=state.session_id,
            message=text,
            cwd=state.session_cwd,
            is_new=state.session_is_new,
            mode=state.mode,
            on_event=on_event,
            on_process_started=lambda p: setattr(state, '_active_process', p),
        )

        if state.session_is_new and not response.startswith("Error:"):
            state.session_is_new = False

        chunks = split_message(response)

        if thinking_id:
            result = await api.edit_message(thinking_id, room_id, chunks[0])
            if result is None:
                await api.delete_message(thinking_id)
                await api.send_message(room_id, chunks[0], parent_id=parent_id)
        else:
            await api.send_message(room_id, chunks[0], parent_id=parent_id)

        for chunk in chunks[1:]:
            await api.send_message(room_id, chunk, parent_id=parent_id)

    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception("Error processing message")
        error_text = "Something went wrong. Try again, or `/cancel` then retry."
        if thinking_id:
            await api.edit_message(thinking_id, room_id, error_text)
        else:
            await api.send_message(room_id, error_text, parent_id=parent_id)
    finally:
        if updater_task is not None:
            updater_task.cancel()
        state._active_process = None
        state._thinking_id = None
        state._last_tool = ""
        state.processing = False


# ---------------------------------------------------------------------------
# Command dispatch
# ---------------------------------------------------------------------------

COMMANDS = {
    "/help": handle_help,
    "/sessions": handle_sessions,
    "/disconnect": handle_disconnect,
    "/status": handle_status,
    "/cancel": handle_cancel,
}

MODE_COMMANDS = {
    "/yolo": PermissionMode.YOLO,
    "/safe": PermissionMode.SAFE,
    "/strict": PermissionMode.STRICT,
}


async def dispatch(api: WebexAPI, room_id: str, text: str) -> None:
    stripped = text.strip()

    if not stripped.startswith("/"):
        await handle_text_message(api, room_id, stripped)
        return

    parts = stripped.split(None, 1)
    command = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ""

    if command == "/resume":
        await handle_resume(api, room_id, arg.strip())
    elif command == "/new":
        await handle_new_session(api, room_id, arg.strip())
    elif command in MODE_COMMANDS:
        await handle_mode(api, room_id, MODE_COMMANDS[command])
    elif command in COMMANDS:
        await COMMANDS[command](api, room_id)
    else:
        await api.send_message(room_id, f"Unknown command: `{command}`\nType `/help` for commands.")


# ---------------------------------------------------------------------------
# Polling loop
# ---------------------------------------------------------------------------

async def _send_startup_welcome(api: WebexAPI) -> str | None:
    try:
        card = {
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "type": "AdaptiveCard",
            "version": "1.2",
            "body": [
                {
                    "type": "Container",
                    "style": "accent",
                    "items": [
                        {
                            "type": "TextBlock",
                            "text": BOT_DISPLAY_NAME,
                            "size": "large",
                            "weight": "bolder",
                        },
                        {
                            "type": "TextBlock",
                            "text": BOT_TAGLINE or "Bot started",
                            "size": "small",
                            "spacing": "small",
                        },
                    ],
                },
                {
                    "type": "TextBlock",
                    "text": "Send a message to begin or type /help.",
                    "wrap": True,
                    "spacing": "medium",
                    "size": "small",
                },
            ],
        }
        fallback = f"{BOT_TAGLINE or 'Bot started'}. Send a message to begin or type /help."
        result = await api.send_card_to_email(WEBEX_USER_EMAIL, card, fallback)
        room_id = result.get("roomId")
        if room_id:
            logger.info("Startup notice sent to %s (room=%s)", WEBEX_USER_EMAIL, room_id[:12])
        return room_id
    except Exception:
        logger.exception("Failed to send startup welcome")
        return None


async def poll_loop(api: WebexAPI) -> None:
    last_seen: dict[str, str] = {}
    initialized_rooms: set[str] = set()

    startup_room = await _send_startup_welcome(api)
    if startup_room:
        initialized_rooms.add(startup_room)
        msgs = await api.list_messages(startup_room, max_messages=1)
        if msgs:
            last_seen[startup_room] = msgs[0]["id"]

    _cleanup_expired_sessions()
    last_cleanup = time.monotonic()
    logger.info("Polling started (interval=%.1fs)", POLL_INTERVAL_SECONDS)

    while True:
        try:
            # Periodic eviction of expired thread sessions + their in-memory state.
            if time.monotonic() - last_cleanup > 3600:
                _cleanup_expired_sessions()
                last_cleanup = time.monotonic()

            rooms = await api.list_direct_rooms(max_rooms=50)

            for room in rooms:
                room_id = room["id"]
                messages = await api.list_messages(room_id, max_messages=10)

                if not messages:
                    continue

                newest_id = messages[0]["id"]

                if room_id not in initialized_rooms:
                    initialized_rooms.add(room_id)
                    last_seen[room_id] = newest_id
                    logger.info("Initialized room %s", room_id[:12])
                    continue

                if last_seen.get(room_id) == newest_id:
                    continue

                new_messages = []
                for msg in messages:
                    if msg["id"] == last_seen.get(room_id):
                        break
                    new_messages.append(msg)

                new_messages.reverse()

                for msg in new_messages:
                    if msg.get("personId") == api.bot_id:
                        continue
                    sender_email = msg.get("personEmail", "")
                    if not is_authorized(sender_email):
                        continue
                    text = msg.get("text", "").strip()
                    if not text:
                        continue

                    logger.info("Message from %s: %s", sender_email, text[:80])
                    await dispatch(api, room_id, text)

                last_seen[room_id] = newest_id

            # --- group spaces (mention-driven) ---
            try:
                group_rooms = await api.list_group_rooms(max_rooms=50)
            except Exception:
                group_rooms = []

            for room in group_rooms:
                room_id = room["id"]
                mentions = await api.list_mentions(room_id, max_messages=10)
                if not mentions:
                    continue

                newest_id = mentions[0]["id"]

                if room_id not in initialized_rooms:
                    initialized_rooms.add(room_id)
                    last_seen[room_id] = newest_id
                    logger.info("Initialized space %s", room_id[:12])
                    continue

                if last_seen.get(room_id) == newest_id:
                    continue

                new_mentions = []
                for msg in mentions:
                    if msg["id"] == last_seen.get(room_id):
                        break
                    new_mentions.append(msg)

                new_mentions.reverse()

                for msg in new_mentions:
                    if msg.get("personId") == api.bot_id:
                        continue
                    await handle_space_mention(api, room_id, msg)

                last_seen[room_id] = newest_id

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
