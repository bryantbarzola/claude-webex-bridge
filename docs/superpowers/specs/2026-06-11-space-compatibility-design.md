# Space Compatibility — Design

**Date:** 2026-06-11
**Status:** Approved (pending spec review)
**Branch:** `space-compat`

## Goal

Make `claude-webex-bridge` work in Webex **group spaces**, not just 1:1 direct
rooms. In spaces the bot responds only when @mentioned, holds threaded
conversations, and keeps a separate Claude Code session per thread.

The proven reference is the `svs-splunk-engineer` bot at
`~/splunk/src/webex_agent`, which is itself a fork of this bridge's
`webex_api.py` that was already made space-native. This design ports its
patterns (mention polling, mention stripping, threaded replies, per-thread
session store) back into the bridge while preserving the existing 1:1
behavior unchanged.

## Decisions (from brainstorming)

| Question | Decision |
|---|---|
| How does the bot know a space message is for it? | **@mention only** |
| Who can command it in a space? | **Anyone in the space** (no email check) |
| Permissions in spaces? | **Keep yolo** — trust the space |
| Which spaces does it watch? | **All group rooms it's a member of** |
| Session model | **Per-thread** (each Webex thread = its own Claude session) |
| Mercury WebSocket (card buttons)? | **Deferred** — out of scope for this work |
| Thread session persistence | **Option A** — persist only the session UUID; rebuild the rest lazily |

## Architecture

```
                 ┌─ direct rooms ──► every message (1:1, single-email auth) ── per-room state (in-memory, today)
poll cycle ──────┤
                 └─ group rooms ──► GET /messages?mentionedPeople=me ──► strip mention ──► thread session
                                                                                          (per-thread, persisted)
```

Two tracks share one poll loop. Direct rooms keep today's exact behavior;
group rooms add the @mention + thread path.

## Components

### 1. Room discovery & trigger
- Each cycle, fetch **both** `type=direct` and `type=group` rooms (sorted by
  last activity), combine, tag each with `roomType`.
- **Direct rooms:** unchanged — process every message via the existing path,
  single-email authorization still applies.
- **Group rooms:** use `GET /messages?roomId=…&mentionedPeople=me` to fetch
  only messages that @mention the bot (server-side filter — quiet and cheap).
  No email check; anyone in the space may command it.

New API method: `WebexAPI.list_group_rooms()` (mirrors `list_direct_rooms`,
`type=group`) and `WebexAPI.list_mentions(room_id)` (adds
`mentionedPeople=me`). Port both from the Splunk bot's `webex_async.py`.

### 2. Threaded conversations
- `thread_id = message.parentId or message.id` — a top-level mention seeds its
  own thread; a reply within a thread carries `parentId`.
- All bot output ("Thinking…", answer, continuation chunks) posts with
  `parentId=thread_id` so concurrent threads stay separate.
- **Mention stripping:** remove the bot name using the message `html` field's
  `<spark-mention>` tags (the plain `text` field truncates long bot names).
  Port `_strip_bot_mention()`.

New API method: `WebexAPI.send_thread_reply(room_id, parent_id, text)`.

### 3. Per-thread session store (Option A)
- A persistent **thread_id → session_id** map: JSON file on disk, 48h TTL,
  periodic cleanup. Port the Splunk bot's `SessionStore`.
- Persists **only the Claude session UUID**. `cwd`, label, and permission mode
  are not persisted per thread — defaults/lazy rebuild.
- @mentioning within a thread auto-resumes that thread's session; no `/resume`
  needed in spaces.

### 4. Unchanged behavior (1:1)
- Direct rooms keep current in-memory per-room state, single-email auth, and
  the full command set (`/resume N`, `/sessions`, `/status`, permission modes).
- Spaces default to **yolo**; no new permission logic.

## Data flow (group space)

1. User @mentions bot in a space.
2. Poll cycle's `list_mentions` returns the message (filtered server-side).
3. `thread_id` resolved; mention stripped from text via `html`.
4. Session store lookup by `thread_id` → existing UUID, or create a new one.
5. "Thinking…" posted as a threaded reply; Claude CLI runs (yolo).
6. Response posted (chunked if needed) as threaded replies under `thread_id`.

## Error handling
- Reuse existing retry/backoff in `_request` (429/5xx) and the adaptive poll
  interval from the Splunk reference.
- Concurrency guard: a second mention while processing gets a threaded
  "still working" reply (existing pattern). Note: today's guard is global
  (one in-flight request per bot). For a first pass we keep it global — it
  matches the Splunk reference and is safe. Per-thread concurrency (allowing
  parallel threads to run at once) is a possible later refinement, not part of
  this work.
- Session store load failures fall back to empty (new sessions), never crash.

## Testing
- Unit: `_strip_bot_mention` (html + fallback), `thread_id` resolution,
  `SessionStore` get/create/TTL/cleanup.
- Manual: @mention in a real space → threaded reply; second thread →
  independent session; restart bot → resume an existing thread; 1:1 DM still
  works unchanged.

## Out of scope
- Mercury WebSocket / clickable card buttons (deferred).
- Per-user permission gating, allowlists, space-owner controls.
- Refactoring the 1:1 path beyond what's needed to add the space track.
