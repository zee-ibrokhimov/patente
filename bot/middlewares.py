"""Middleware: make sure a user record exists, and hand handlers their language.

Every handler needs both, so doing it once here keeps them from each re-deriving
it — and keeps the "register on first contact" rule in exactly one place.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, User

from bot.api_client import ApiClient, ApiError
from bot.i18n import normalise

log = logging.getLogger(__name__)


class UserMiddleware(BaseMiddleware):
    def __init__(self, api: ApiClient):
        self.api = api

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user: User | None = data.get("event_from_user")
        if tg_user is None:
            return await handler(event, data)

        try:
            user = await self.api.get_user(tg_user.id)
        except ApiError as exc:
            if exc.status != 404:
                raise
            # Telegram's UI language is a decent first guess; the user picks
            # properly during onboarding.
            user = await self.api.register(tg_user.id, normalise(tg_user.language_code))

        data["user"] = user
        data["lang"] = user["lang"]
        data["api"] = self.api
        return await handler(event, data)


class ErrorLoggingMiddleware(BaseMiddleware):
    """Never leave a tapped button spinning, even when the API is down."""

    async def __call__(self, handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        try:
            return await handler(event, data)
        except Exception:
            log.exception("handler failed for %s", type(event).__name__)
            from bot.i18n import t

            lang = data.get("lang", "en")
            try:
                if isinstance(event, CallbackQuery):
                    await event.answer(t(lang, "error"), show_alert=True)
                elif isinstance(event, Message):
                    await event.answer(t(lang, "error"))
            except Exception:
                pass
            return None
