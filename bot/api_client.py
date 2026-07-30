"""Thin HTTP client over the Core API.

The whole of the bot's data access. No SQLAlchemy import anywhere under bot/, no
entitlement decision, no Leitner arithmetic — if any of that leaked in here, the
Mini App would end up with a second implementation and the two would disagree
within a month (plan §6.1).
"""

from __future__ import annotations

from typing import Any

import httpx

from shared.config import settings


# The default 10s is right for every other call. An explanation request may be the one
# that generates it, which means a model call with a page of statute in the prompt.
EXPLANATION_TIMEOUT = 90.0


class ApiError(RuntimeError):
    def __init__(self, status: int, detail: str):
        super().__init__(f"API {status}: {detail}")
        self.status = status
        self.detail = detail


class ApiClient:
    def __init__(self, base_url: str | None = None, client: httpx.AsyncClient | None = None):
        self._client = client or httpx.AsyncClient(
            base_url=base_url or settings.api_base_url, timeout=10.0
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, url: str, **kw) -> Any:
        response = await self._client.request(method, url, **kw)
        if response.status_code >= 400:
            detail = response.text
            try:
                detail = response.json().get("detail", detail)
            except Exception:
                pass
            raise ApiError(response.status_code, detail)
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    # --- users -------------------------------------------------------------
    async def register(self, chat_id: int, lang: str | None = None) -> dict:
        return await self._request("POST", "/users", json={"chat_id": chat_id, "lang": lang})

    async def get_user(self, chat_id: int) -> dict:
        return await self._request("GET", f"/users/{chat_id}")

    async def update_user(self, chat_id: int, **fields) -> dict:
        return await self._request("PATCH", f"/users/{chat_id}", json=fields)

    async def grant_pass(self, chat_id: int, days: int, reason: str = "") -> dict:
        return await self._request(
            "POST", f"/users/{chat_id}/pass", json={"days": days, "reason": reason}
        )

    async def delete_user(self, chat_id: int) -> None:
        await self._request("DELETE", f"/users/{chat_id}")

    # --- quiz --------------------------------------------------------------
    async def stats(self, chat_id: int) -> dict:
        return await self._request("GET", f"/users/{chat_id}/stats")

