import asyncio
import logging
import os
import shutil

from config import CLI_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)


def _clean_env() -> dict[str, str]:
    """Return a copy of the environment with CLAUDECODE stripped to avoid nesting detection."""
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    return env


async def send_message(
    session_id: str,
    message: str,
    cwd: str,
    skip_permissions: bool = True,
    on_process_started: object = None,
) -> str:
    """Send a message to a Claude Code session via CLI and return the response."""
    claude_path = shutil.which("claude")
    if claude_path is None:
        return "Error: 'claude' CLI not found on PATH. Make sure Claude Code is installed."

    cmd = [
        claude_path,
        "--print",
        "--output-format", "text",
        "--resume", session_id,
    ]
    if skip_permissions:
        cmd.append("--dangerously-skip-permissions")
    cmd.append("--")
    cmd.append(message)

    logger.info("Running: %s (cwd=%s)", " ".join(cmd[:6]) + " ...", cwd)

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=_clean_env(),
        )
    except FileNotFoundError:
        return "Error: 'claude' CLI not found on PATH. Make sure Claude Code is installed."
    except OSError as e:
        return f"Error starting CLI: {e}"

    if on_process_started is not None:
        on_process_started(process)

    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=CLI_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return f"Error: CLI timed out after {CLI_TIMEOUT_SECONDS} seconds. The process was killed."

    stdout_text = stdout.decode("utf-8", errors="replace").strip()
    stderr_text = stderr.decode("utf-8", errors="replace").strip()

    if process.returncode != 0:
        error_msg = f"Claude encountered an error (exit code {process.returncode}). Try sending your message again, or disconnect and reconnect."
        if stderr_text:
            # Log stderr server-side only (may contain secrets)
            logger.error("CLI stderr: %s", stderr_text[:1000])
        if "expired" in stderr_text.lower() or "credential" in stderr_text.lower():
            error_msg += "\n\nThis may be an AWS credentials issue. Check your credentials."
        return error_msg

    if not stdout_text:
        return "Claude completed the request but returned no output."

    return stdout_text
