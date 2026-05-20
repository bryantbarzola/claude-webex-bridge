# Claude Webex Bridge

Resume and interact with [Claude Code](https://docs.anthropic.com/en/docs/claude-code) sessions from Webex Teams.

## Prerequisites

- **Python 3.9+**
- **[Claude Code](https://docs.anthropic.com/en/docs/claude-code)** installed and on your PATH (`claude --version` should work)
- At least one prior Claude Code session (the bot reads from `~/.claude/history.jsonl`)

## Quick Start

```bash
cd claude-webex-bridge
python3 run.py
```

That's it. The script checks prerequisites, creates a virtual environment, installs dependencies, walks you through Webex bot token setup, and starts the bot. On subsequent runs it skips setup and goes straight to launch.

> **macOS note:** You can also double-click `Run.command`, but macOS Gatekeeper may block it. If that happens, just use `python3 run.py` from Terminal instead.

### Run in Background (survives terminal close)

```bash
nohup python3 run.py > bot.log 2>&1 &
```

This keeps the bot running even after you close the terminal. To stop it later:

```bash
kill $(pgrep -f "python3 run.py")
```

### Alternative: Step-by-Step Setup

If you prefer to set things up manually:

#### 1. Create a Webex Bot

1. Go to [developer.webex.com](https://developer.webex.com) and sign in
2. Avatar menu -> "My Webex Apps" -> "Create a New App" -> "Create a Bot"
3. Fill in bot name and username, click "Add Bot"
4. Copy the Bot Access Token (shown only once)

#### 2. Run Setup Script

```bash
./setup.sh
```

#### 3. Start the Bot

```bash
./start.sh
```

The bot runs in the background. Use these commands to manage it:

```bash
./status.sh   # Check if bot is running
./logs.sh     # View recent logs
./logs.sh -f  # Follow logs in real-time
./stop.sh     # Stop the bot
./restart.sh  # Restart the bot
```

## Manual Setup

If you prefer not to use the automated scripts:

<details>
<summary>Click to expand manual setup instructions</summary>

### 1. Create `.env` file

```bash
cp .env.example .env
```

Edit `.env` with your values:

```
WEBEX_BOT_TOKEN=your-bot-access-token
WEBEX_USER_EMAIL=your-email@example.com
```

### 2. Install Dependencies

```bash
pip3 install -r requirements.txt
```

### 3. Run Manually

```bash
python3 bot.py
```

You should see `Bot authenticated as: ...` in the logs.

</details>

## Commands

| Command | Description |
|---|---|
| `/help` | Show commands and current status |
| `/new [dir]` | Start a new session (defaults to home directory) |
| `/sessions` | List recent sessions as a numbered list |
| `/resume N` | Resume session N from the list |
| `/resume` | Quick-resume the most recent session |
| `/disconnect` | Disconnect from current session |
| `/status` | Show connection status and permission mode |
| `/yolo` | Auto-approve all tool use (default) |
| `/safe` | Ask before each tool use (via Webex) |
| `/strict` | Read-only tools only |
| `/cancel` | Cancel a running command |

Just type a message to start chatting — no need to `/resume` first. The bot auto-creates a session.

## Permission Modes

| Mode | Behavior |
|---|---|
| **yolo** (default) | Claude executes all tools without asking. Best for trusted environments. |
| **safe** | Claude asks permission before each tool via Webex message. You reply yes/no. |
| **strict** | Only read-only tools (Read, Glob, Grep) are allowed. Everything else is auto-denied. |

Switch modes anytime with `/yolo`, `/safe`, or `/strict`.

## Customizing Claude's Behavior

The bot launches Claude Code sessions on the host machine. To customize how Claude behaves in those sessions, edit `~/.claude/CLAUDE.md` on the machine running the bot. This is Claude Code's standard system prompt — anything you put there shapes all sessions.

Example `~/.claude/CLAUDE.md`:

```markdown
# My Assistant

You are a helpful assistant for managing my lab environment.

## Behavior
- Keep responses concise — I'm reading on mobile
- Propose before executing destructive actions
- If something fails, report the exact error
```

This file is **not** included in the repo — it's personal to each deployment.

## Architecture

```
bot.py          # Polling loop + command dispatch + message relay
claude_cli.py   # Spawn-per-message CLI wrapper with stream-json event parsing
webex_api.py    # Async httpx wrapper for Webex REST API
auth.py         # Email-based authorization check
config.py       # Environment variables + constants
sessions.py     # Claude Code session discovery (reads ~/.claude/history.jsonl)
```

### How It Works

Each message spawns a `claude` CLI process with `--output-format stream-json`. The bot reads events (tool use, text output) line-by-line while the process runs, updating the "Thinking..." message with what Claude is doing. When the process exits, the final response replaces the thinking message.

This gives you:
- **Event visibility** — see which tools Claude is using mid-turn
- **Resilience** — each message is independent; a crash doesn't cascade
- **Session context** — Claude Code maintains conversation history via `--resume`
- **Cancellation** — `/cancel` kills the process cleanly

### Why Polling

- No public URL needed (webhooks require ngrok or similar — unnecessary for a personal bot)
- ~2.5s latency is negligible when CLI calls take seconds-to-minutes
- Works behind firewalls and NAT

### Key Design Decisions

- **Session discovery** reads Claude Code's own history and project files — no separate database.
- **Auto-connect** — just type to chat; bot creates a session automatically.
- **Byte-aware message splitting** respects Webex's 7,439-byte limit by splitting on UTF-8 byte length.
- **"Thinking..." pattern** sends a placeholder, then edits it with the response (falls back to new message if edit fails).
- **Concurrency guard** prevents overlapping CLI calls — a second message while processing gets a "still processing" reply.
- **Rate-limit handling** retries on 429 with `Retry-After` header, up to 3 times.
- **CLI timeout** kills the process after 5 minutes.

## Deployment (systemd)

For always-on deployment on a Linux server:

```bash
# Create systemd user service
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/claude-webex-bridge.service << 'EOF'
[Unit]
Description=Claude Webex Bridge Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/path/to/claude-webex-bridge
ExecStart=/path/to/claude-webex-bridge/venv/bin/python3 bot.py
Restart=on-failure
RestartSec=10
EnvironmentFile=/path/to/claude-webex-bridge/.env

[Install]
WantedBy=default.target
EOF

# Enable and start
loginctl enable-linger $USER
systemctl --user daemon-reload
systemctl --user enable --now claude-webex-bridge

# Management
systemctl --user status claude-webex-bridge
systemctl --user restart claude-webex-bridge
journalctl --user -u claude-webex-bridge -f
```

The shell scripts (`start.sh`, `stop.sh`, etc.) auto-detect systemd and use it when available.

## Security

Only the email matching `WEBEX_USER_EMAIL` (case-insensitive) can interact with the bot. Messages from other users are silently ignored and logged as warnings.
