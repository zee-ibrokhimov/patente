"""aiogram entry point.

Long polling, one process, thin client. The bot holds no database handle and
makes no entitlement decision — every screen it draws comes from a Core API
response (plan §6.1).
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, BotCommandScopeDefault

from bot.api_client import ApiClient
from bot.handlers import build_router
from bot.i18n import t
from bot.middlewares import ErrorLoggingMiddleware, UserMiddleware
from shared.config import settings
from shared.constants import UI_LANGUAGES

log = logging.getLogger(__name__)

COMMANDS = [
    ("quiz", "cmd_quiz"),
    ("stats", "cmd_stats"),
    ("settings", "cmd_settings"),
    ("help", "cmd_help"),
    ("privacy", "cmd_privacy"),
    ("delete", "cmd_delete"),
]


async def publish_commands(bot: Bot) -> None:
    """Telegram shows the menu in the user's own client language when we register
    a list per language_code, so this is worth doing for all three."""
    for lang in UI_LANGUAGES:
        commands = [
            BotCommand(command=name, description=t(lang, key)) for name, key in COMMANDS
        ]
        await bot.set_my_commands(
            commands, scope=BotCommandScopeDefault(), language_code=lang
        )


def build_dispatcher(api: ApiClient) -> Dispatcher:
    dispatcher = Dispatcher()
    for observer in (dispatcher.message, dispatcher.callback_query):
        # Outer wraps everything, including user lookup, so an API outage still
        # answers the callback instead of leaving the button spinning.
        observer.outer_middleware(ErrorLoggingMiddleware())
        observer.middleware(UserMiddleware(api))
    dispatcher.include_router(build_router())
    return dispatcher


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    if not settings.bot_token:
        raise SystemExit(
            "no bot token — set BOT_TOKEN_DEV (or BOT_TOKEN_PROD with ENV=prod) in .env"
        )

    bot = Bot(
        settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    api = ApiClient()
    dispatcher = build_dispatcher(api)

    me = await bot.get_me()
    log.info("starting @%s in %s against %s", me.username, settings.env, settings.api_base_url)
    await publish_commands(bot)

    try:
        await dispatcher.start_polling(bot)
    finally:
        await api.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
