from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from config import CLI_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Permission modes
# ---------------------------------------------------------------------------

class PermissionMode:
    YOLO = "yolo"
    SAFE = "safe"
    STRICT = "strict"

STRICT_ALLOWED_TOOLS = [
    "Read", "Glob", "Grep", "Bash(read-only)",
]


# ---------------------------------------------------------------------------
# Stream events (emitted during turn via on_event callback)
# ---------------------------------------------------------------------------

@dataclass
class StreamEvent:
    type: str


@dataclass
class ToolUseEvent(StreamEvent):
    tool_name: str = ""
    tool_id: str = ""
    input_preview: str = ""

    def __init__(self, tool_name: str = "", tool_id: str = "", input_preview: str = ""):
        super().__init__(type="tool_use")
        self.tool_name = tool_name
        self.tool_id = tool_id
        self.input_preview = input_preview


@dataclass
class PermissionEvent(StreamEvent):
    tool_name: str = ""
    tool_id: str = ""
    description: str = ""

    def __init__(self, tool_name: str = "", tool_id: str = "", description: str = ""):
        super().__init__(type="permission")
        self.tool_name = tool_name
        self.tool_id = tool_id
        self.description = description


@dataclass
class TextEvent(StreamEvent):
    text: str = ""

    def __init__(self, text: str = ""):
        super().__init__(type="text")
        self.text = text


@dataclass
class ResultEvent(StreamEvent):
    text: str = ""
    duration_ms: int = 0
    cost_usd: float = 0.0

    def __init__(self, text: str = "", duration_ms: int = 0, cost_usd: float = 0.0):
        super().__init__(type="result")
        self.text = text
        self.duration_ms = duration_ms
        self.cost_usd = cost_usd


# ---------------------------------------------------------------------------
# Core: send a message (spawn per turn, stream events)
# ---------------------------------------------------------------------------

def generate_session_id() -> str:
    return str(uuid.uuid4())


def _clean_env() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    return env


def _build_cmd(
    session_id: str,
    message: str,
    is_new: bool,
    mode: str,
) -> list[str]:
    claude_path = shutil.which("claude")
    if claude_path is None:
        raise FileNotFoundError("'claude' CLI not found on PATH")

    cmd = [
        claude_path,
        "--print",
        "--output-format", "stream-json",
        "--verbose",
    ]

    if is_new:
        cmd.extend(["--session-id", session_id])
    else:
        cmd.extend(["--resume", session_id])

    if mode == PermissionMode.YOLO:
        cmd.append("--dangerously-skip-permissions")
    elif mode == PermissionMode.STRICT:
        cmd.extend(["--allowedTools", ",".join(STRICT_ALLOWED_TOOLS)])
    # SAFE mode: no permission flags — Claude will emit permission requests

    cmd.append("--")
    cmd.append(message)
    return cmd


async def send_message(
    session_id: str,
    message: str,
    cwd: str,
    is_new: bool = False,
    mode: str = PermissionMode.YOLO,
    on_event: Callable[[StreamEvent], Any] | None = None,
    on_permission: Callable[[PermissionEvent], Any] | None = None,
    on_process_started: Callable[[asyncio.subprocess.Process], None] | None = None,
) -> str:
    """
    Send a message to Claude Code. Spawns a process, streams events, returns final text.

    Args:
        session_id: Session UUID (--resume or --session-id)
        message: User message text
        cwd: Working directory for Claude
        is_new: True for first message (uses --session-id), False for resume
        mode: Permission mode (yolo/safe/strict)
        on_event: Called for each stream event (tool_use, text) — for UI updates
        on_permission: Called when Claude requests permission (safe mode only).
                       Must return True (approve) or False (deny).
        on_process_started: Called with the process object (for /cancel)
    """
    try:
        cmd = _build_cmd(session_id, message, is_new, mode)
    except FileNotFoundError as e:
        return f"Error: {e}"

    logger.info("CLI: %s (cwd=%s, mode=%s)", " ".join(cmd[:6]) + " ...", cwd, mode)

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=cwd,
            env=_clean_env(),
        )
    except FileNotFoundError:
        return "Error: 'claude' CLI not found on PATH."
    except OSError as e:
        return f"Error starting CLI: {e}"

    if on_process_started:
        on_process_started(process)

    text_parts: list[str] = []
    result_text = ""

    try:
        result_text = await asyncio.wait_for(
            _stream_events(process, text_parts, on_event, on_permission),
            timeout=CLI_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return f"Error: Claude timed out after {CLI_TIMEOUT_SECONDS} seconds."
    except asyncio.CancelledError:
        process.kill()
        await process.wait()
        raise

    await process.wait()

    if process.returncode != 0 and not text_parts and not result_text:
        return "Claude encountered an error. Try sending your message again."

    return result_text or "".join(text_parts) or "Claude completed but returned no output."


async def _stream_events(
    process: asyncio.subprocess.Process,
    text_parts: list[str],
    on_event: Callable[[StreamEvent], Any] | None,
    on_permission: Callable[[PermissionEvent], Any] | None,
) -> str:
    """Read stream-json lines from stdout, dispatch events, return result text."""
    assert process.stdout

    result_text = ""

    while True:
        line = await process.stdout.readline()
        if not line:
            break

        raw = line.decode("utf-8", errors="replace").strip()
        if not raw:
            continue

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue

        event_type = data.get("type", "")

        if event_type == "assistant":
            content = data.get("message", {}).get("content", [])
            for block in content:
                block_type = block.get("type", "")
                if block_type == "text":
                    text = block.get("text", "")
                    text_parts.append(text)
                    if on_event:
                        evt = TextEvent(text=text)
                        await _call(on_event, evt)
                elif block_type == "tool_use":
                    tool_name = block.get("name", "")
                    tool_id = block.get("id", "")
                    tool_input = block.get("input", {})
                    preview = _tool_preview(tool_name, tool_input)
                    if on_event:
                        evt = ToolUseEvent(tool_name=tool_name, tool_id=tool_id, input_preview=preview)
                        await _call(on_event, evt)

        elif event_type == "result":
            result_text = data.get("result", "")

    return result_text


def _tool_preview(tool_name: str, tool_input: dict) -> str:
    """Generate a short preview of what a tool is doing."""
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        return cmd[:80] if cmd else ""
    elif tool_name == "Read":
        return tool_input.get("file_path", "")
    elif tool_name == "Write" or tool_name == "Edit":
        return tool_input.get("file_path", "")
    elif tool_name == "Grep":
        return tool_input.get("pattern", "")
    return json.dumps(tool_input)[:60] if tool_input else ""


async def _call(fn: Callable, *args: Any) -> Any:
    """Call a function, awaiting if it's async."""
    result = fn(*args)
    if asyncio.iscoroutine(result):
        return await result
    return result
