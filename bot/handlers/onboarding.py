"""Onboarding: language, one short orientation, then into the app.

The plan is specific about this (§2): no wall of text before the user has done
anything. So /start is two taps — pick a language, read three lines, open the app.
Everything else (privacy, how the boxes work, settings) is behind /help and only
surfaces if asked for.

Drilling itself lives in the Mini App now, so the last step of onboarding is the
hand-off rather than a first question.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, Message

from bot import keyboards
from bot.api_client import ApiClient
from bot.callbacks import SetLanguage
from bot.i18n import t

router = Router(name="onboarding")


@router.message(CommandStart())
async def start(message: Message, user: dict, lang: str):
    if user["onboarded_at"] is None:
        await message.answer(
            t(lang, "choose_language"), reply_markup=keyboards.language_picker()
        )
        return

    # Returning user: no preamble, straight back to the app.
    await message.answer(t(lang, "study_in_app"), reply_markup=keyboards.open_app(lang))


@router.callback_query(SetLanguage.filter())
async def pick_language(query: CallbackQuery, callback_data: SetLanguage, api: ApiClient):
    user = await api.update_user(
        query.from_user.id, lang=callback_data.code, onboarded=True
    )
    lang = user["lang"]

    await query.message.edit_text(f"{t(lang, 'welcome')}\n\n{t(lang, 'disclaimer')}")
    await query.answer()
    await query.message.answer(
        t(lang, "study_in_app"), reply_markup=keyboards.open_app(lang)
    )


@router.callback_query(F.data == "s:language")
async def change_language(query: CallbackQuery, lang: str):
    await query.message.edit_text(
        t(lang, "choose_language"), reply_markup=keyboards.language_picker()
    )
    await query.answer()
