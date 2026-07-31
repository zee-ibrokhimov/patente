"""What the bot kept when drilling moved to the Mini App: progress and money.

Plan §6.2 splits the surfaces this way on purpose — the app is for sustained study,
chat is for onboarding, notifications and **payment**. A Mini App selling digital
goods sits closer to Telegram's Stars-only rule and to Apple's review guidelines, so
the subscription screen belongs here even though everything it unlocks is used there.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from bot import keyboards, render
from bot.api_client import ApiClient
from bot.callbacks import Simple
from bot.i18n import t
from shared.config import settings

router = Router(name="progress")


def _can_subscribe() -> bool:
    """Payments are live only once Tribute is actually configured.

    The check is the webhook secret AND at least one checkout link. It used to require
    `tribute_product_1m`, a DIGITAL PRODUCT id — but a subscription payload carries no
    product id at all (the tier comes from `period`), so a subscription-based setup
    could never have opened this gate. The link is the right test: it is exactly what
    the button needs in order to lead somewhere.
    """
    return settings.can_sell


@router.message(Command("stats"))
async def stats(message: Message, lang: str, api: ApiClient):
    data = await api.stats(message.from_user.id)
    await message.answer(render.stats(data, lang), reply_markup=keyboards.open_app(lang))


@router.message(Command("plan"))
async def plan(message: Message, user: dict, lang: str):
    # ONE decision for the text and the button. They used to take `can_subscribe`
    # separately — a deployment fact meaning "a checkout link exists" — so the keyboard
    # attached Subscribe under every /plan, including the trial message that says the
    # subscription renews automatically and the free-tier pitch shown to people who are
    # already Premium through the channel.
    sell = render.selling(user, can_subscribe=_can_subscribe())
    await message.answer(
        render.plan(user, lang, can_subscribe=sell),
        reply_markup=keyboards.plan_actions(lang, can_subscribe=sell),
    )


@router.callback_query(Simple.filter(F.action == "subscribe"))
async def subscribe(query: CallbackQuery, lang: str):
    """Legacy. The tier buttons are URL buttons now and Telegram opens them itself, so
    nothing sends this callback any more. Kept because an old message still sitting in
    someone's chat history carries the old button, and a callback with no handler shows
    a spinner that never resolves."""
    await query.answer(t(lang, "payments_not_live"), show_alert=True)
