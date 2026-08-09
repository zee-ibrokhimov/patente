from __future__ import annotations

import logging

from aiogram.types import InlineKeyboardMarkup, WebAppInfo
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import SetLanguage, Simple
from bot.i18n import LANGUAGE_NAMES, t
from shared.config import settings
from shared.constants import TRANSLATION_LANGUAGES, UI_LANGUAGES

log = logging.getLogger(__name__)


def language_picker() -> InlineKeyboardMarkup:
    """Never index LANGUAGE_NAMES directly.

    This is the first screen of onboarding and the change-language flow. A missing
    entry here is a KeyError that takes down /start for every new user — the whole
    acquisition funnel — triggered by a one-line edit to a constant in another file.
    A language with no display name should show its code, not crash.
    """
    kb = InlineKeyboardBuilder()
    for code in UI_LANGUAGES:
        kb.button(
            text=LANGUAGE_NAMES.get(code, code.upper()),
            callback_data=SetLanguage(code=code),
        )
    kb.adjust(1)
    return kb.as_markup()


def open_app(lang: str) -> InlineKeyboardMarkup | None:
    """The hand-off to the Mini App, where drilling now happens.

    Returns None when no URL is configured. Telegram rejects a web_app button whose
    url is not https, and an unconfigured deployment would then fail on every /start —
    a bot that answers without a button is a far better failure than one that answers
    with an error.
    """
    if not settings.webapp_url.startswith("https://"):
        log.warning(
            "WEBAPP_URL is not an https URL (%r) — sending without the open-app button",
            settings.webapp_url,
        )
        return None
    kb = InlineKeyboardBuilder()
    kb.button(text=t(lang, "open_app"), web_app=WebAppInfo(url=settings.webapp_url))
    kb.adjust(1)
    return kb.as_markup()


def main_menu(lang: str) -> InlineKeyboardMarkup:
    """Everything the bot can do, as buttons.

    Nobody types slash-commands. They are registered and they work, but a learner who has
    just been handed a chat window does not think "I wonder if /stats exists" — so the
    features behind them were, for practical purposes, invisible. The commands stay for
    people who like them; this is the surface for everyone else.

    Open the app comes first and alone, because it is what the product IS. The rest are two
    to a row: they are errands, not the point.
    """
    kb = InlineKeyboardBuilder()
    if settings.webapp_url.startswith("https://"):
        kb.button(text=t(lang, "open_app"), web_app=WebAppInfo(url=settings.webapp_url))
    kb.button(text=t(lang, "menu_stats"), callback_data="m:stats")
    kb.button(text=t(lang, "menu_plan"), callback_data="m:plan")
    kb.button(text=t(lang, "menu_settings"), callback_data="m:settings")
    kb.button(text=t(lang, "menu_help"), callback_data="m:help")
    # 1 across for the app, then 2+2 — the web_app button is the only one worth a full row.
    kb.adjust(1, 2, 2) if settings.webapp_url.startswith("https://") else kb.adjust(2, 2)
    return kb.as_markup()


def plan_actions(lang: str, *, can_subscribe: bool) -> InlineKeyboardMarkup | None:
    """One button: open a chat with the person who takes the money.

    THERE IS NO CHECKOUT ANY MORE. Payment moved off Tribute on 2026-08-09 to a direct
    arrangement — the learner messages the owner, they agree terms, and access is granted by
    hand. So the button's job changed from "open a payment page" to "start the conversation",
    and the two fail in very different ways: a broken checkout takes money and delivers
    nothing, a broken handle simply does not open a chat.

    `can_subscribe` still means "should this person be sold to at all" and is still
    respected — it is `render.selling`, which is False for anyone who already has Premium and
    for a trialist whose card is about to be charged. Its OTHER old meaning, "a checkout link
    exists", is gone with the checkout.

    Returns None when there is nobody to message, because a Subscribe button that opens
    nothing reads as a broken product rather than an unfinished one — which was true of the
    version that pointed at an unconfigured Tribute link, and is the one property of that
    design worth keeping.
    """
    if not can_subscribe:
        return None

    handle = settings.sales_handle
    if not handle:
        return None

    kb = InlineKeyboardBuilder()
    kb.button(text=t(lang, "btn_subscribe"), url=f"https://t.me/{handle}")
    kb.adjust(1)
    return kb.as_markup()


def settings_menu(lang: str, translations_on: bool) -> InlineKeyboardMarkup:
    """The translations toggle only appears for a language we actually translate into.

    Italian is a UI language but not a translation target — correctly, since the
    question is already Italian. Offering the toggle anyway let a user switch it on and
    be told it was active while no translation could ever appear. The same trap is
    waiting for any new UI language added ahead of its translations.
    """
    kb = InlineKeyboardBuilder()
    if lang in TRANSLATION_LANGUAGES:
        state = t(lang, "state_on" if translations_on else "state_off")
        kb.button(
            text=t(lang, "btn_toggle_translations", state=state),
            callback_data=Simple(action="toggle_translations"),
        )
    kb.button(text=t(lang, "btn_change_language"), callback_data=Simple(action="language"))
    kb.adjust(1)
    return kb.as_markup()


def confirm_delete(lang: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=t(lang, "btn_delete_yes"), callback_data=Simple(action="delete_yes"))
    kb.button(text=t(lang, "btn_cancel"), callback_data=Simple(action="delete_no"))
    kb.adjust(1)
    return kb.as_markup()
