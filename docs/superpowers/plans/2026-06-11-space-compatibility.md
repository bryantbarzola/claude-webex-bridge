# Space Compatibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `claude-webex-bridge` respond in Webex group spaces — @mention-triggered, threaded conversations, one Claude Code session per thread — while leaving 1:1 DM behavior unchanged.

**Architecture:** One poll loop, two tracks. Direct rooms keep today's exact path (every message, single-email auth, in-memory per-room state). Group rooms add a track that fetches only @mention messages (`mentionedPeople=me`), strips the bot name, resolves a per-thread Claude session from a persistent JSON store, and posts threaded replies. The Claude-run + reply machinery is extracted into one shared helper so both tracks use it (DRY).

**Tech Stack:** Python 3.11+, `asyncio`, `httpx`, `pytest`, `unittest.mock`. Patterns ported from the proven `svs-splunk-engineer` bot (`~/splunk/src/webex_agent`).

---

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `session_store.py` | **Create** | Persistent `thread_id → session_id` map (JSON, 48h TTL, cleanup) |
| `webex_api.py` | Modify | Cache `bot_display_name`; add `list_group_rooms`, `list_mentions`, `send_thread_reply` |
| `bot.py` | Modify | `_strip_bot_mention`, `_get_thread_id`, extract `_run_and_reply`, add `handle_thread_message`, extend `poll_loop` |
| `tests/test_session_store.py` | **Create** | SessionStore behavior |
| `tests/test_webex_api.py` | Modify | New API methods |
| `tests/test_bot.py` | Modify | Mention strip + thread id |

---

## Task 1: SessionStore (persistent thread → session map)

**Files:**
- Create: `session_store.py`
- Test: `tests/test_session_store.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_session_store.py
"""Tests for session_store.py: thread→session mapping with TTL."""

import os
import sys

os.environ.setdefault("WEBEX_BOT_TOKEN", "test-token")
os.environ.setdefault("WEBEX_USER_EMAIL", "test@example.com")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pathlib import Path

import pytest

from session_store import SessionStore


@pytest.fixture
def store(tmp_path):
    return SessionStore(path=tmp_path / "sessions.json")


def test_get_missing_returns_none(store):
    assert store.get("thread-1") is None


def test_create_then_get(store):
    store.create("thread-1", "sess-abc")
    assert store.get("thread-1") == "sess-abc"


def test_persists_across_instances(tmp_path):
    p = tmp_path / "sessions.json"
    SessionStore(path=p).create("t1", "s1")
    assert SessionStore(path=p).get("t1") == "s1"


def test_expired_entry_returns_none(store):
    store.create("t1", "s1")
    # Force the entry's created time into the distant past
    store._data["t1"]["created"] = 0
    assert store.get("t1") is None


def test_cleanup_removes_expired(store):
    store.create("t1", "s1")
    store.create("t2", "s2")
    store._data["t1"]["created"] = 0
    store.cleanup()
    assert "t1" not in store._data
    assert "t2" in store._data


def test_corrupt_file_falls_back_to_empty(tmp_path):
    p = tmp_path / "sessions.json"
    p.write_text("{ not valid json")
    store = SessionStore(path=p)
    assert store.get("anything") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_session_store.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'session_store'`

- [ ] **Step 3: Write minimal implementation**

```python
# session_store.py
"""Thread-to-session mapping for Claude Code CLI sessions.

Maps Webex thread IDs (parentId or root message id) to Claude Code session
UUIDs. Persisted as JSON so sessions survive bot restarts. Per the design,
only the session UUID is stored — cwd/label/mode are not persisted per thread.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_PATH = Path.home() / ".claude-webex-bridge" / "thread_sessions.json"
TTL_SECONDS = 48 * 3600  # 48 hours


class SessionStore:
    """Persistent thread_id → session_id mapping with TTL."""

    def __init__(self, path: Path = DEFAULT_PATH) -> None:
        self._path = Path(path)
        self._data: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text())
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Failed to load session store: %s", e)
                self._data = {}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._data, indent=2))

    def get(self, thread_id: str) -> str | None:
        """Return session_id for a thread, or None if missing/expired."""
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
        """Store a new thread→session mapping."""
        now = time.time()
        self._data[thread_id] = {
            "session_id": session_id,
            "created": now,
            "last_used": now,
        }
        self._save()

    def cleanup(self) -> None:
        """Remove expired entries."""
        now = time.time()
        expired = [k for k, v in self._data.items() if now - v["created"] > TTL_SECONDS]
        for k in expired:
            del self._data[k]
        if expired:
            self._save()
            logger.info("Cleaned up %d expired thread sessions", len(expired))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_session_store.py -v`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
git add session_store.py tests/test_session_store.py
git commit -m "feat: add persistent thread→session store"
```

---

## Task 2: Webex API methods for spaces

**Files:**
- Modify: `webex_api.py` (cache `bot_display_name` in `start()`; add three methods after `list_messages`)
- Test: `tests/test_webex_api.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_webex_api.py`:

```python
class TestSpaceMethods:
    @pytest.mark.asyncio
    async def test_list_group_rooms_params(self, api):
        api._request = AsyncMock(return_value={"items": [{"id": "r1"}]})
        rooms = await api.list_group_rooms(max_rooms=25)
        api._request.assert_awaited_once_with(
            "GET", "/rooms",
            params={"type": "group", "sortBy": "lastactivity", "max": "25"},
        )
        assert rooms == [{"id": "r1"}]

    @pytest.mark.asyncio
    async def test_list_mentions_params(self, api):
        api._request = AsyncMock(return_value={"items": []})
        await api.list_mentions("room-1", max_messages=10)
        api._request.assert_awaited_once_with(
            "GET", "/messages",
            params={"roomId": "room-1", "mentionedPeople": "me", "max": "10"},
        )

    @pytest.mark.asyncio
    async def test_send_thread_reply_payload(self, api):
        api._request = AsyncMock(return_value={"id": "m1"})
        await api.send_thread_reply("room-1", "parent-1", "hello")
        api._request.assert_awaited_once_with(
            "POST", "/messages",
            json={"roomId": "room-1", "parentId": "parent-1", "markdown": "hello"},
        )
```

If `import pytest` lacks asyncio support, the file already runs async tests — confirm `pytest-asyncio` or the existing `asyncio.run` pattern. If the existing tests wrap calls in `asyncio.run(...)` instead of `@pytest.mark.asyncio`, mirror that style instead:

```python
def test_list_mentions_params(api):
    api._request = AsyncMock(return_value={"items": []})
    asyncio.run(api.list_mentions("room-1", max_messages=10))
    api._request.assert_awaited_once_with(
        "GET", "/messages",
        params={"roomId": "room-1", "mentionedPeople": "me", "max": "10"},
    )
```

(Check the top of `tests/test_webex_api.py` for which pattern existing async tests use and match it.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_webex_api.py -k Space -v`
Expected: FAIL with `AttributeError: 'WebexAPI' object has no attribute 'list_group_rooms'`

- [ ] **Step 3: Write minimal implementation**

In `webex_api.py`, add `bot_display_name` to `__init__` and cache it in `start()`:

```python
    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self.bot_id: str | None = None
        self.bot_display_name: str = ""
```

In `start()`, replace the display-name handling:

```python
        data = await self._request("GET", "/people/me")
        self.bot_id = data["id"]
        self.bot_display_name = data.get("displayName", "")
        logger.info("Bot authenticated as: %s (id=%s)", self.bot_display_name or "Unknown", self.bot_id)
```

Add these methods immediately after `list_messages`:

```python
    async def list_group_rooms(self, max_rooms: int = 50) -> list[dict]:
        """List group (space) rooms sorted by last activity."""
        data = await self._request(
            "GET",
            "/rooms",
            params={"type": "group", "sortBy": "lastactivity", "max": str(max_rooms)},
        )
        return data.get("items", [])

    async def list_mentions(self, room_id: str, max_messages: int = 10) -> list[dict]:
        """List messages in a room that @mention the bot (newest first)."""
        data = await self._request(
            "GET",
            "/messages",
            params={"roomId": room_id, "mentionedPeople": "me", "max": str(max_messages)},
        )
        return data.get("items", [])

    async def send_thread_reply(self, room_id: str, parent_id: str, text: str) -> dict:
        """Send a markdown message as a threaded reply under parent_id."""
        return await self._request(
            "POST",
            "/messages",
            json={"roomId": room_id, "parentId": parent_id, "markdown": text},
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_webex_api.py -k Space -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add webex_api.py tests/test_webex_api.py
git commit -m "feat: add group-room, mention, and thread-reply Webex API methods"
```

---

## Task 3: Mention stripping and thread-id helpers

**Files:**
- Modify: `bot.py` (add two module-level helpers near the other `_` helpers, e.g. after `_short_path`)
- Test: `tests/test_bot.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_bot.py`:

```python
from bot import _strip_bot_mention, _get_thread_id


class TestStripBotMention:
    def test_strips_spark_mention_from_html(self):
        html = '<p><spark-mention data-object-id="123">Claude</spark-mention> resume the lab</p>'
        out = _strip_bot_mention("Claude resume the lab", html, "Claude Code Bridge")
        assert out == "resume the lab"

    def test_html_with_no_text_after_mention(self):
        html = '<p><spark-mention data-object-id="123">Claude</spark-mention></p>'
        out = _strip_bot_mention("Claude", html, "Claude Code Bridge")
        assert out == ""

    def test_fallback_strips_known_bot_name(self):
        out = _strip_bot_mention("Claude Code Bridge hello there", "", "Claude Code Bridge")
        assert out == "hello there"

    def test_fallback_strips_first_word_when_name_truncated(self):
        out = _strip_bot_mention("Claude do the thing", "", "Claude Code Bridge")
        assert out == "do the thing"


class TestGetThreadId:
    def test_uses_parent_id_when_present(self):
        assert _get_thread_id({"id": "m2", "parentId": "root-1"}) == "root-1"

    def test_uses_message_id_when_no_parent(self):
        assert _get_thread_id({"id": "m1"}) == "m1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_bot.py -k "StripBotMention or GetThreadId" -v`
Expected: FAIL with `ImportError: cannot import name '_strip_bot_mention'`

- [ ] **Step 3: Write minimal implementation**

Add to `bot.py` (after `_short_path`, before the card builders). First add `import re` to the top imports (it is not currently imported — place it after `import logging`):

```python
def _strip_bot_mention(text: str, html: str, bot_name: str) -> str:
    """Remove the bot @mention from a message.

    Prefers the HTML field's <spark-mention> tags (reliable), since the plain
    text field can contain a truncated bot name. Falls back to stripping the
    known bot name or the first word.
    """
    if html:
        cleaned = re.sub(r"<spark-mention[^>]*>.*?</spark-mention>", "", html)
        cleaned = re.sub(r"<[^>]+>", "", cleaned)
        return cleaned.strip()

    stripped = text.strip()
    if bot_name and stripped.lower().startswith(bot_name.lower()):
        return stripped[len(bot_name):].strip()
    parts = stripped.split(None, 1)
    return parts[1] if len(parts) > 1 else ""


def _get_thread_id(message: dict) -> str:
    """Thread id for a message: its parentId, or its own id if top-level."""
    return message.get("parentId") or message["id"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_bot.py -k "StripBotMention or GetThreadId" -v`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
git add bot.py tests/test_bot.py
git commit -m "feat: add mention-stripping and thread-id helpers"
```

---

## Task 4: Extract shared `_run_and_reply` helper (refactor, behavior-preserving)

**Files:**
- Modify: `bot.py` (`handle_text_message` — extract its core into `_run_and_reply`)

This refactor introduces an optional `parent_id`: when set, all sends go through `send_thread_reply`; when `None`, behavior is byte-for-byte the same as today. Existing `tests/test_bot.py` plus a manual 1:1 smoke test guard against regressions.

- [ ] **Step 1: Add the helper and rewrite `handle_text_message` to call it**

Replace the existing `handle_text_message` (lines ~570–645) with the following two functions:

```python
async def _run_and_reply(
    api: WebexAPI,
    room_id: str,
    state: BotState,
    *,
    session_id: str,
    message: str,
    cwd: str,
    is_new: bool,
    mode: str,
    parent_id: str | None = None,
) -> None:
    """Run one Claude CLI turn and post the reply.

    If parent_id is set, "Thinking..." and all reply chunks are posted as
    threaded replies; otherwise they are posted as top-level messages.
    Mutates state.session_is_new to False on a successful new session.
    """

    async def _send(text: str) -> dict:
        if parent_id:
            return await api.send_thread_reply(room_id, parent_id, text)
        return await api.send_message(room_id, text)

    state.processing = True
    state._last_tool = ""
    thinking_id = None
    updater_task = None
    tool_event = asyncio.Event()

    try:
        thinking = await _send("Thinking...")
        thinking_id = thinking.get("id")
        state._thinking_id = thinking_id

        if thinking_id:
            updater_task = asyncio.create_task(_update_thinking(api, state, room_id, tool_event))

        def on_event(event: StreamEvent) -> None:
            if isinstance(event, ToolUseEvent):
                state._last_tool = event.tool_name
                tool_event.set()

        response = await cli_send_message(
            session_id=session_id,
            message=message,
            cwd=cwd,
            is_new=is_new,
            mode=mode,
            on_event=on_event,
            on_process_started=lambda p: setattr(state, "_active_process", p),
        )

        if is_new and not response.startswith("Error:"):
            state.session_is_new = False

        chunks = split_message(response)

        if thinking_id:
            result = await api.edit_message(thinking_id, room_id, chunks[0])
            if result is None:
                await api.delete_message(thinking_id)
                await _send(chunks[0])
        else:
            await _send(chunks[0])

        for chunk in chunks[1:]:
            await _send(chunk)

    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception("Error processing message")
        error_text = "Something went wrong. Try again, or `/cancel` then retry."
        if thinking_id:
            await api.edit_message(thinking_id, room_id, error_text)
        else:
            await _send(error_text)
    finally:
        if updater_task is not None:
            updater_task.cancel()
        state._active_process = None
        state._thinking_id = None
        state._last_tool = ""
        state.processing = False


async def handle_text_message(api: WebexAPI, room_id: str, text: str) -> None:
    state = get_state(room_id)

    if state.session_id is None:
        state.session_id = generate_session_id()
        state.session_is_new = True
        state.session_cwd = str(Path.home())

    if state.processing:
        await api.send_message(room_id, "Still processing. Use `/cancel` to abort.")
        return

    await _run_and_reply(
        api,
        room_id,
        state,
        session_id=state.session_id,
        message=text,
        cwd=state.session_cwd,
        is_new=state.session_is_new,
        mode=state.mode,
        parent_id=None,
    )
```

- [ ] **Step 2: Run the full existing suite to verify no regression**

Run: `python -m pytest tests/ -v`
Expected: PASS (all previously-passing tests still pass)

- [ ] **Step 3: Commit**

```bash
git add bot.py
git commit -m "refactor: extract _run_and_reply shared by 1:1 and (soon) thread paths"
```

---

## Task 5: `handle_thread_message` for group spaces

**Files:**
- Modify: `bot.py` (add `handle_thread_message` after `handle_text_message`)
- Test: `tests/test_bot.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_bot.py`:

```python
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import bot as bot_module


class TestHandleThreadMessage:
    def test_new_thread_creates_session_and_threads_reply(self, tmp_path):
        from session_store import SessionStore

        api = MagicMock()
        api.bot_display_name = "Claude Code Bridge"
        api.send_thread_reply = AsyncMock(return_value={"id": "think-1"})
        api.edit_message = AsyncMock(return_value={"id": "think-1"})
        api.delete_message = AsyncMock()

        store = SessionStore(path=tmp_path / "s.json")
        msg = {
            "id": "root-1",
            "text": "Claude what is up",
            "html": '<p><spark-mention>Claude</spark-mention> what is up</p>',
        }

        with patch.object(bot_module, "cli_send_message", new=AsyncMock(return_value="hi there")):
            asyncio.run(bot_module.handle_thread_message(api, "room-9", msg, store))

        # A session was persisted for this thread
        assert store.get("root-1") is not None
        # Reply was threaded under the root message
        assert api.send_thread_reply.await_args_list[0].args[1] == "root-1"

    def test_existing_thread_resumes_session(self, tmp_path):
        from session_store import SessionStore

        api = MagicMock()
        api.bot_display_name = "Claude Code Bridge"
        api.send_thread_reply = AsyncMock(return_value={"id": "think-2"})
        api.edit_message = AsyncMock(return_value={"id": "think-2"})
        api.delete_message = AsyncMock()

        store = SessionStore(path=tmp_path / "s.json")
        store.create("root-1", "existing-sess")
        msg = {"id": "m2", "parentId": "root-1", "text": "Claude more", "html": "<p><spark-mention>Claude</spark-mention> more</p>"}

        captured = {}

        async def fake_cli(**kwargs):
            captured.update(kwargs)
            return "ok"

        with patch.object(bot_module, "cli_send_message", new=fake_cli):
            asyncio.run(bot_module.handle_thread_message(api, "room-9", msg, store))

        assert captured["session_id"] == "existing-sess"
        assert captured["is_new"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_bot.py -k HandleThreadMessage -v`
Expected: FAIL with `AttributeError: module 'bot' has no attribute 'handle_thread_message'`

- [ ] **Step 3: Write minimal implementation**

Add to `bot.py` after `handle_text_message`. Import `SessionStore` at the top (`from session_store import SessionStore`):

```python
async def handle_thread_message(
    api: WebexAPI,
    room_id: str,
    message: dict,
    session_store: SessionStore,
) -> None:
    """Handle an @mention in a group space as a threaded Claude session."""
    thread_id = _get_thread_id(message)
    user_text = _strip_bot_mention(
        message.get("text", ""), message.get("html", ""), api.bot_display_name
    )
    if not user_text:
        await api.send_thread_reply(
            room_id, thread_id, "Mention me with a request and I'll get to work."
        )
        return

    # Per-room state holds the processing guard / cancel handle / thinking id.
    state = get_state(room_id)
    if state.processing:
        await api.send_thread_reply(
            room_id, thread_id,
            "I'm still working on a previous request. Please wait a moment.",
        )
        return

    session_id = session_store.get(thread_id)
    is_new = session_id is None
    if is_new:
        session_id = generate_session_id()
        session_store.create(thread_id, session_id)
        logger.info("New thread session %s for thread %s", session_id[:12], thread_id[:16])
    else:
        logger.info("Resuming thread session %s for thread %s", session_id[:12], thread_id[:16])

    await _run_and_reply(
        api,
        room_id,
        state,
        session_id=session_id,
        message=user_text,
        cwd=str(Path.home()),  # Option A: cwd not persisted per thread; default to home
        is_new=is_new,
        mode=PermissionMode.YOLO,  # spaces default to yolo (trust the space)
        parent_id=thread_id,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_bot.py -k HandleThreadMessage -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add bot.py tests/test_bot.py
git commit -m "feat: handle group-space mentions as threaded Claude sessions"
```

---

## Task 6: Extend the poll loop to discover and route group rooms

**Files:**
- Modify: `bot.py` (`poll_loop` and `async_main`)

The current `poll_loop` iterates `list_direct_rooms` and processes every message. We add a second pass over `list_group_rooms`, fetching only mentions and routing them to `handle_thread_message`. A module-level `SessionStore` is created in `poll_loop`. Per-room `last_seen` tracking already exists and is reused for group rooms (keyed by room id, same dict).

- [ ] **Step 1: Add group-room polling**

In `poll_loop`, after the `SessionStore` is created (add it near the top of the function, before the `while True` loop):

```python
    session_store = SessionStore()
    last_cleanup = time.time()
    CLEANUP_INTERVAL = 3600
```

Inside the `while True:` loop, after the existing direct-room `for room in rooms:` block completes (just before the final `await asyncio.sleep(POLL_INTERVAL_SECONDS)`), add:

```python
            # --- Group spaces: @mention-triggered, threaded sessions ---
            group_rooms = await api.list_group_rooms(max_rooms=50)
            for room in group_rooms:
                room_id = room["id"]
                mentions = await api.list_mentions(room_id, max_messages=10)
                if not mentions:
                    continue

                newest_id = mentions[0]["id"]
                if room_id not in initialized_rooms:
                    initialized_rooms.add(room_id)
                    last_seen[room_id] = newest_id
                    logger.info("Initialized group room %s", room_id[:12])
                    continue

                if last_seen.get(room_id) == newest_id:
                    continue

                new_mentions = []
                for msg in mentions:
                    if msg["id"] == last_seen.get(room_id):
                        break
                    if msg.get("personId") == api.bot_id:
                        continue
                    new_mentions.append(msg)

                new_mentions.reverse()
                for msg in new_mentions:
                    logger.info("Mention in space %s: %s", room_id[:12], msg.get("text", "")[:80])
                    await handle_thread_message(api, room_id, msg, session_store)

                last_seen[room_id] = newest_id

            # Periodic cleanup of expired thread sessions
            if time.time() - last_cleanup > CLEANUP_INTERVAL:
                session_store.cleanup()
                last_cleanup = time.time()
```

- [ ] **Step 2: Verify it imports and the suite still passes**

Run: `python -c "import bot" && python -m pytest tests/ -v`
Expected: import OK; all tests PASS

- [ ] **Step 3: Commit**

```bash
git add bot.py
git commit -m "feat: poll group spaces for mentions and route to thread handler"
```

---

## Task 7: Manual verification against a real space

**Files:** none (manual smoke test on `svs-truist-ai` or local Mac bot)

- [ ] **Step 1: Run the bot locally with the dev token**

Run: `python run.py` (or the project's start command)
Expected: log line `Bot authenticated as: <name>`, then `Polling started`.

- [ ] **Step 2: 1:1 regression check**

Send a normal DM to the bot. Expected: it replies exactly as before (no thread, no @mention needed).

- [ ] **Step 3: Space — new thread**

Add the bot to a group space, then `@<bot> what host am I on?`.
Expected: a **threaded** "Thinking…" then a threaded reply. `~/.claude-webex-bridge/thread_sessions.json` now has an entry for that thread.

- [ ] **Step 4: Space — thread continuity**

Reply inside that thread `@<bot> and what's the uptime?`.
Expected: reply stays in the same thread and Claude has context from step 3 (same session resumed).

- [ ] **Step 5: Space — second independent thread**

Start a new top-level `@<bot> ...` in the same space.
Expected: a separate thread with its own session; does not mix with the first.

- [ ] **Step 6: Restart persistence check**

Restart the bot, then reply in the first thread again.
Expected: the session resumes from disk (context preserved).

- [ ] **Step 7: Commit any fixes, then the plan is complete**

```bash
git add -A && git commit -m "fix: manual-verification adjustments for space support"  # only if changes were needed
```

---

## Notes for the implementer
- **DRY:** `_run_and_reply` is the single source of truth for the Claude-run + reply machinery. Don't duplicate it in `handle_thread_message`.
- **YAGNI:** No Mercury WebSocket, no per-user auth, no per-thread permission modes — all explicitly out of scope.
- **Webex `html` field:** mention messages include an `html` field with `<spark-mention>` tags; `_strip_bot_mention` relies on it and falls back to text-based stripping. This is the proven approach from the Splunk bot.
- **Concurrency guard stays global per room** (`BotState.processing`). Per-thread parallelism is a possible future refinement, not this work.
