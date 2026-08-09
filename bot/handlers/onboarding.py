"""Onboarding: language, one short orientation, then into the app.

The plan is specific about this (§2): no wall of text before the user has done
anything. So /start is two taps — pick a language, read three lines, open the app.
Everything else (privacy, how the boxes work, settings) is behind /help and only
surfaces if asked for.

Drilling itself lives in the Mini App now, so the last step of onboarding is the
hand-off rather than a first question.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.types import (
    CallbackQuery,
    MenuButtonWebApp,
    Message,
    WebAppInfo,
)

from bot import keyboards, render
from bot.api_client import ApiClient
from bot.callbacks import SetLanguage
from bot.i18n import t
from shared.config import settings

log = logging.getLogger(__name__)
router = Router(name="onboarding")


@router.message(CommandStart(deep_link=True, magic=F.args == "plan"))
async def start_plan(message: Message, user: dict, lang: str):
    """Arrived from the Mini App's Buy button — `t.me/<bot>?start=plan`.

    Answers with the prices immediately. Without this the button would drop a learner who
    had just decided to pay into an empty chat, where they would have to guess that
    /plan exists. Deep links are the only way a Mini App can hand off with intent
    attached, since Telegram tells the bot nothing about why the app closed.
    """
    from bot import render
    from bot.handlers.progress import _can_subscribe

    # Same single decision as /plan — see bot/handlers/progress.py. Someone who arrived
    # here from the Mini App's Buy button can still already be Premium: the app's own
    # paywalls read `me.premium`, but this deep link is reachable from an old message.
    sell = render.selling(user, can_subscribe=_can_subscribe())
    await message.answer(
        render.plan(user, lang, can_subscribe=sell),
        reply_markup=keyboards.plan_actions(lang, can_subscribe=sell),
    )


@router.message(CommandStart())
async def start(message: Message, user: dict, lang: str):
    if user["onboarded_at"] is None:
        await message.answer(
            t(lang, "choose_language"), reply_markup=keyboards.language_picker()
        )
        return

    # Returning user: no preamble, straight back to the app.
    await offer_the_app(message.bot, message.chat.id)
    await message.answer(t(lang, "study_in_app"), reply_markup=keyboards.main_menu(lang))


@router.callback_query(SetLanguage.filter())
async def pick_language(query: CallbackQuery, callback_data: SetLanguage, api: ApiClient):
    user = await api.update_user(
        query.from_user.id, lang=callback_data.code, onboarded=True
    )
    lang = user["lang"]

    await query.message.edit_text(f"{t(lang, 'welcome')}\n\n{t(lang, 'disclaimer')}")
    await query.answer()
    await offer_the_app(query.bot, query.message.chat.id)
    await query.message.answer(
        t(lang, "study_in_app"), reply_markup=keyboards.main_menu(lang)
    )


@router.callback_query(F.data == "s:language")
async def change_language(query: CallbackQuery, lang: str):
    await query.message.edit_text(
        t(lang, "choose_language"), reply_markup=keyboards.language_picker()
    )
    await query.answer()


# --- the buttons behind the main menu ----------------------------------------
#
# Each one answers with exactly what its slash-command answers. Deliberately delegating
# rather than duplicating: two renderings of "your stats" drift, and the version behind the
# button is the one most people will see.

@router.callback_query(F.data.startswith("m:"))
async def main_menu_choice(query: CallbackQuery, user: dict, lang: str, api: ApiClient):
    from bot.handlers import misc, progress

    action = query.data.split(":", 1)[1]
    await query.answer()
    message = query.message

    if action == "stats":
        data = await api.stats(query.from_user.id)
        await message.answer(render.stats(data, lang),
                             reply_markup=keyboards.open_app(lang))
    elif action == "plan":
        sell = render.selling(user, can_subscribe=progress._can_subscribe())
        await message.answer(
            render.plan(user, lang, can_subscribe=sell),
            reply_markup=keyboards.plan_actions(lang, can_subscribe=sell))
    elif action == "settings":
        await message.answer(render.settings(user, lang),
                             reply_markup=keyboards.settings_menu(
                                 lang, user["translations_on"]))
    elif action == "help":
        await message.answer(t(lang, "help"), reply_markup=keyboards.main_menu(lang))
    else:
        log.warning("unknown main-menu action %r", action)


async def offer_the_app(bot, chat_id: int) -> None:
    """Make this chat's menu button open the Mini App instead of listing commands.

    Telegram shows "Menu" — the commands list — until a bot says otherwise, and that is
    where a new learner looks first. It is the single most valuable button in the chat and
    it was pointing at a list of slash-commands nobody types.

    PER CHAT, not the default. Setting the DEFAULT menu button is accepted by the API and
    then silently does nothing here: `getChatMenuButton` keeps reporting `{"type":
    "commands"}` afterwards, because the bot has a command list registered and BotFather's
    own Menu Button setting outranks the API. Per chat is the call that actually holds —
    verified against the live bot.

    Never raises. A menu button is a nicety; failing /start over it is not.
    """
    if not settings.webapp_url.startswith("https://"):
        return
    try:
        await bot.set_chat_menu_button(
            chat_id=chat_id,
            menu_button=MenuButtonWebApp(
                text=t(lang_for_menu(), "open_app_short"),
                web_app=WebAppInfo(url=settings.webapp_url),
            ),
        )
    except Exception:                                                 # noqa: BLE001
        log.warning("could not set the menu button for %s", chat_id, exc_info=True)


def lang_for_menu() -> str:
    """The menu button's label is set once per chat and Telegram does not re-ask, so it
    cannot follow a language change. English is the least-wrong single choice for a word
    that sits next to a text field in every client."""
    return "en"
