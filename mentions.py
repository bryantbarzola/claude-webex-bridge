"""Pure helpers for Webex space-mode message handling (no I/O, unit-tested)."""
import re

_SPARK_MENTION = re.compile(r"<spark-mention[^>]*>.*?</spark-mention>", re.DOTALL)
_HTML_TAG = re.compile(r"<[^>]+>")


def strip_mention(text: str, html: str, bot_display_name: str) -> str:
    """Remove the bot @mention from a space message.

    Primary: strip <spark-mention> tags from the html field (name-agnostic).
    Fallback (html missing): strip the bot's display name if the text starts
    with it, else drop the first whitespace-delimited token.
    """
    if html:
        cleaned = _SPARK_MENTION.sub("", html)
        cleaned = _HTML_TAG.sub("", cleaned)
        return cleaned.strip()

    stripped = text.strip()
    if bot_display_name and stripped.lower().startswith(bot_display_name.lower()):
        return stripped[len(bot_display_name):].strip()
    parts = stripped.split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ""


def thread_id_of(message: dict) -> str:
    """Thread root for a message: its parentId, or its own id if top-level."""
    return message.get("parentId") or message["id"]
