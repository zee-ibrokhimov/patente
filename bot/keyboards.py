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


def plan_actions(lang: str, *, can_subscribe: bool) -> InlineKeyboardMarkup | None:
    """One button per tier, each a direct link to that tier's Tribute checkout.

    `can_subscribe` is False until at least one link is configured. A Buy button that
    leads nowhere is worse than no button — it reads as a broken product rather than an
    unfinished one.

    Three buttons rather than one "Subscribe" that then asks which: /plan has just
    listed the three prices, so asking again is a step that exists only because the
    keyboard could not be bothered. Ordered shortest to longest, matching the message
    above it, with the featured tier marked — the same order in both, so the eye does
    not have to re-read.
    """
    from shared.constants import TIER_DAYS, TIER_FEATURED, TIER_PRICE_CENTS

    links = settings.tribute_links
    if not can_subscribe or not links:
        return None

    kb = InlineKeyboardBuilder()
    for tier in sorted(TIER_DAYS, key=lambda x: TIER_DAYS[x]):
        url = links.get(tier)
        if not url:
            continue
        # The same key render.plan() uses for the price line, so a button can never
        # disagree with the message immediately above it.
        label = t(lang, f"plan_{tier.replace('pass_', '')}")
        price = f"{TIER_PRICE_CENTS[tier] // 100}.{TIER_PRICE_CENTS[tier] % 100:02d}"
        star = " ⭐" if tier == TIER_FEATURED else ""
        kb.button(text=f"{label} — €{price}{star}", url=url)
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
