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

    async def delete_user(self, chat_id: int) -> None:
        await self._request("DELETE", f"/users/{chat_id}")

    # --- quiz --------------------------------------------------------------
    async def next_question(
        self, chat_id: int, topic_id: int | None = None, exclude_id: int | None = None
    ) -> dict:
        params = {k: v for k, v in
                  {"topic_id": topic_id, "exclude_id": exclude_id}.items() if v is not None}
        return await self._request("GET", f"/users/{chat_id}/next-question", params=params)

    async def answer(self, chat_id: int, question_id: int, answer: bool) -> dict:
        return await self._request(
            "POST", f"/users/{chat_id}/answers",
            json={"question_id": question_id, "answer": answer},
        )

    async def translation(self, chat_id: int, question_id: int) -> dict:
        """May be the call that produces it, so it gets the long timeout too."""
        return await self._request(
            "POST", f"/users/{chat_id}/questions/{question_id}/translation",
            timeout=EXPLANATION_TIMEOUT,
        )

    async def explanation(self, chat_id: int, question_id: int) -> dict:
        """The fallback when warming has not landed. May take several seconds, because
        it may be the call that produces the explanation."""
        return await self._request(
            "POST", f"/users/{chat_id}/questions/{question_id}/explanation",
            timeout=EXPLANATION_TIMEOUT,
        )

    async def stats(self, chat_id: int) -> dict:
        return await self._request("GET", f"/users/{chat_id}/stats")

    async def report(self, chat_id: int, question_id: int) -> dict:
        return await self._request(
            "POST", f"/users/{chat_id}/reports", json={"question_id": question_id}
        )

    # --- figures -----------------------------------------------------------
    async def figure_bytes(self, name: str) -> bytes:
        response = await self._client.get(f"/figures/{name}")
        response.raise_for_status()
        return response.content

    async def cache_file_id(self, name: str, file_id: str) -> None:
        await self._request("PUT", f"/figures/{name}/file-id", json={"file_id": file_id})
