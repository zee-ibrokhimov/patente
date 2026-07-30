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

    @property
    def is_transient(self) -> bool:
        """Worth retrying, and worth telling the user so.

        status 0 means the request never reached the API at all (DNS, connect, timeout);
        5xx means it arrived and the API failed. Both are "try again in a moment". A 4xx
        is not — retrying a bad request produces the same bad request, and telling a user
        to retry teaches them the bot is flaky when it is actually working correctly.
        """
        return self.status == 0 or self.status >= 500


class ApiClient:
    def __init__(self, base_url: str | None = None, client: httpx.AsyncClient | None = None):
        self._client = client or httpx.AsyncClient(
            base_url=base_url or settings.api_base_url, timeout=10.0
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, url: str, **kw) -> Any:
        # httpx raises transport and timeout failures as its own exception types, which
        # previously escaped this client untouched — so an API outage surfaced as a
        # different exception than an API error and every caller had to know about both.
        # status 0 marks "never got an answer".
        try:
            response = await self._client.request(method, url, **kw)
        except httpx.HTTPError as exc:
            raise ApiError(0, f"{type(exc).__name__}: {exc}") from exc
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

