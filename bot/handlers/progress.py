"""What the bot kept when drilling moved to the Mini App: progress and money.

Plan §6.2 splits the surfaces this way on purpose — the app is for sustained study,
chat is for onboarding, notifications and **payment**. A Mini App selling digital
goods sits closer to Telegram's Stars-only rule and to Apple's review guidelines, so
the subscription screen belongs here even though everything it unlocks is used there.
"""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from bot import keyboards, render
from bot.api_client import ApiClient
from bot.callbacks import Simple
from bot.i18n import t
from shared.config import settings

router = Router(name="progress")


def _can_subscribe() -> bool:
    """Payments are live only once Tribute is actually configured. Without this the
    Buy button would open nothing, which reads as broken rather than unfinished."""
    return bool(settings.tribute_webhook_secret and settings.tribute_product_1m)


@router.message(Command("stats"))
async def stats(message: Message, lang: str, api: ApiClient):
    data = await api.stats(message.from_user.id)
    await message.answer(render.stats(data, lang), reply_markup=keyboards.open_app(lang))


@router.message(Command("plan"))
async def plan(message: Message, user: dict, lang: str):
    await message.answer(
        render.plan(user, lang, can_subscribe=_can_subscribe()),
        reply_markup=keyboards.plan_actions(lang, can_subscribe=_can_subscribe()),
    )


@router.callback_query(Simple.filter(lambda c: c.action == "subscribe"))
async def subscribe(query: CallbackQuery, lang: str):
    """Reachable only when Tribute is configured, since the button is otherwise not
    rendered. Until the checkout link is wired this says so plainly."""
    await query.answer(t(lang, "payments_not_live"), show_alert=True)
