from __future__ import annotations

import asyncio
import logging

import httpx

from config import WEBEX_BASE_URL, WEBEX_BOT_TOKEN

logger = logging.getLogger(__name__)

MAX_RETRIES = 3


class WebexAPI:
    """Thin async wrapper around the Webex REST API using httpx."""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self.bot_id: str | None = None

    async def start(self) -> None:
        """Initialize the HTTP client, verify the token, and cache bot_id."""
        self._client = httpx.AsyncClient(
            base_url=WEBEX_BASE_URL,
            headers={
                "Authorization": f"Bearer {WEBEX_BOT_TOKEN}",
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )
        data = await self._request("GET", "/people/me")
        self.bot_id = data["id"]
        display_name = data.get("displayName", "Unknown")
        logger.info("Bot authenticated as: %s (id=%s)", display_name, self.bot_id)

    async def close(self) -> None:
        """Close the httpx client."""
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _request(
        self,
        method: str,
        path: str,
        json: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        """Make an API request with rate-limit and transient-error retry handling."""
        if self._client is None:
            raise RuntimeError("Call start() before making requests")

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = await self._client.request(method, path, json=json, params=params)
            except httpx.RequestError as exc:
                # Transient network errors (connect, read, DNS, etc.)
                if attempt < MAX_RETRIES:
                    wait = min(2 ** attempt, 30)
                    logger.warning(
                        "Request error (attempt %d/%d): %s — retrying in %ds",
                        attempt, MAX_RETRIES, exc, wait,
                    )
                    await asyncio.sleep(wait)
                    continue
                raise

            if response.status_code == 429:
                try:
                    retry_after = min(int(response.headers.get("Retry-After", "5")), 60)
                except ValueError:
                    retry_after = 5
                logger.warning(
                    "Rate limited (attempt %d/%d), retrying in %ds",
                    attempt, MAX_RETRIES, retry_after,
                )
                await asyncio.sleep(retry_after)
                continue

            if response.status_code == 401:
                logger.error("Authentication failed (401). Check WEBEX_BOT_TOKEN.")
                raise SystemExit("Fatal: Webex API returned 401 Unauthorized.")

            # Retry on transient server errors (5xx)
            if response.status_code >= 500 and attempt < MAX_RETRIES:
                wait = min(2 ** attempt, 30)
                logger.warning(
                    "Server error %d (attempt %d/%d), retrying in %ds",
                    response.status_code, attempt, MAX_RETRIES, wait,
                )
                await asyncio.sleep(wait)
                continue

            response.raise_for_status()
            return response.json()

        # Exhausted retries
        logger.error("Retries exhausted after %d attempts", MAX_RETRIES)
        raise httpx.HTTPStatusError(
            "Retries exhausted",
            request=response.request,
            response=response,
        )

    async def list_direct_rooms(self, max_rooms: int = 50) -> list[dict]:
        """List direct (1:1) rooms sorted by last activity."""
        data = await self._request(
            "GET",
            "/rooms",
            params={"type": "direct", "sortBy": "lastactivity", "max": str(max_rooms)},
        )
        return data.get("items", [])

    async def list_messages(self, room_id: str, max_messages: int = 10) -> list[dict]:
        """List messages in a room (newest first)."""
        data = await self._request(
            "GET",
            "/messages",
            params={"roomId": room_id, "max": str(max_messages)},
        )
        return data.get("items", [])

    async def send_message(self, room_id: str, text: str) -> dict:
        """Send a text message to a room."""
        return await self._request(
            "POST",
            "/messages",
            json={"roomId": room_id, "markdown": text},
        )

    async def send_card_message(self, room_id: str, card: dict, fallback_text: str) -> dict:
        """Send a message with an Adaptive Card attachment."""
        return await self._request(
            "POST",
            "/messages",
            json={
                "roomId": room_id,
                "text": fallback_text,
                "attachments": [
                    {
                        "contentType": "application/vnd.microsoft.card.adaptive",
                        "content": card,
                    }
                ],
            },
        )

    async def send_card_to_email(self, email: str, card: dict, fallback_text: str) -> dict:
        """Send a message with an Adaptive Card to a person by email (creates 1:1 room if needed)."""
        return await self._request(
            "POST",
            "/messages",
            json={
                "toPersonEmail": email,
                "text": fallback_text,
                "attachments": [
                    {
                        "contentType": "application/vnd.microsoft.card.adaptive",
                        "content": card,
                    }
                ],
            },
        )

    async def edit_message(self, message_id: str, room_id: str, text: str) -> dict | None:
        """Edit an existing message. Returns None on failure (caller should fallback)."""
        try:
            return await self._request(
                "PUT",
                f"/messages/{message_id}",
                json={"roomId": room_id, "markdown": text},
            )
        except (httpx.HTTPStatusError, httpx.RequestError) as e:
            logger.warning("Failed to edit message %s: %s", message_id, e)
            return None
