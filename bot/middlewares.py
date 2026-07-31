"""Middleware: make sure a user record exists, and hand handlers their language.

Every handler needs both, so doing it once here keeps them from each re-deriving
it — and keeps the "register on first contact" rule in exactly one place.
"""

from __future__ import annotations

import logging
import sys
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
            #
            # The /start payload rides along on registration rather than being recorded
            # by the handler, because this is the ONE moment it can be captured: the
            # middleware creates the user, and by the time /start runs the row exists
            # with no source on it. Attribution is unreconstructable after the fact.
            user = await self.api.register(
                tg_user.id, normalise(tg_user.language_code), _start_payload(event)
            )

        data["user"] = user
        data["lang"] = user["lang"]
        data["api"] = self.api
        return await handler(event, data)


def _start_payload(event: TelegramObject) -> str | None:
    """The text after /start, if this event is one.

    `t.me/quizpatente_bot?start=tg_uzbeks_italy` arrives as the message "/start
    tg_uzbeks_italy". Anything else — a callback, a photo, any other command — has no
    payload and returns None rather than guessing.
    """
    text = getattr(event, "text", None)
    if not text or not text.startswith("/start"):
        return None
    parts = text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else None


class ErrorLoggingMiddleware(BaseMiddleware):
    """Never leave a tapped button spinning, even when the API is down."""

    async def __call__(self, handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        try:
            return await handler(event, data)
        except Exception:
            log.exception("handler failed for %s", type(event).__name__)
            from bot.i18n import t

            # This middleware is registered OUTER and UserMiddleware INNER, and aiogram
            # gives the inner chain its own data dict — so `data["lang"]`, set by
            # UserMiddleware, never reaches this except block. Reading only that meant
            # every error message in this bot was English for every user, always, at the
            # one moment a user has already lost confidence.
            #
            # Telegram's own client language is the right fallback: it rides on the
            # event, it needs no API call (the API may be exactly what is broken), and
            # it is what onboarding already uses for its first guess.
            lang = data.get("lang")
            if not lang:
                tg_user: User | None = data.get("event_from_user")
                lang = normalise(getattr(tg_user, "language_code", None))

            # "The service is down, try in a minute" and "that request was invalid" are
            # different messages. Saying the same thing for both teaches a user that the
            # bot is flaky even when it is working correctly.
            exc = sys.exc_info()[1]
            key = "error_transient" if (
                isinstance(exc, ApiError) and exc.is_transient
            ) else "error"
            try:
                if isinstance(event, CallbackQuery):
                    await event.answer(t(lang, key), show_alert=True)
                elif isinstance(event, Message):
                    await event.answer(t(lang, key))
            except Exception:
                pass
            return None
