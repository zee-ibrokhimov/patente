"""Onboarding: language, one short orientation, first question.

The plan is specific about this (§2): a first question inside 60 seconds and no
wall of text before the user has answered anything. So /start is two taps —
pick a language, read three lines, answer. Everything else (privacy, how the
boxes work, settings) is behind /help and only surfaces if asked for.
"""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, Message

from bot import keyboards, quiz_flow
from bot.api_client import ApiClient
from bot.callbacks import SetLanguage
from bot.i18n import t

router = Router(name="onboarding")


@router.message(CommandStart())
async def start(message: Message, user: dict, lang: str, api: ApiClient, bot: Bot):
    if user["onboarded_at"] is None:
        await message.answer(t(lang, "choose_language"), reply_markup=keyboards.language_picker())
        return

    # Returning user: no preamble, straight back to drilling.
    await quiz_flow.send_question(bot, api, message.chat.id, lang)


@router.callback_query(SetLanguage.filter())
async def pick_language(
    query: CallbackQuery, callback_data: SetLanguage, api: ApiClient, bot: Bot
):
    user = await api.update_user(
        query.from_user.id, lang=callback_data.code, onboarded=True
    )
    lang = user["lang"]

    await query.message.edit_text(
        f"{t(lang, 'welcome')}\n\n{t(lang, 'disclaimer')}"
    )
    await query.answer()
    await quiz_flow.send_question(bot, api, query.message.chat.id, lang)


@router.callback_query(F.data == "s:language")
async def change_language(query: CallbackQuery, lang: str):
    await query.message.edit_text(
        t(lang, "choose_language"), reply_markup=keyboards.language_picker()
    )
    await query.answer()
