from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import Answer, NextQuestion, ReportBad, SetLanguage, Simple
from bot.i18n import LANGUAGE_NAMES, t
from shared.constants import UI_LANGUAGES


def language_picker() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for code in UI_LANGUAGES:
        kb.button(text=LANGUAGE_NAMES[code], callback_data=SetLanguage(code=code))
    kb.adjust(1)
    return kb.as_markup()


def answer_buttons(question_id: int, lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text=t(lang, "btn_vero"),
            callback_data=Answer(qid=question_id, value=True).pack(),
        ),
        InlineKeyboardButton(
            text=t(lang, "btn_falso"),
            callback_data=Answer(qid=question_id, value=False).pack(),
        ),
    ]])


def after_answer(question_id: int, lang: str, *, locked: bool, explained: bool) -> InlineKeyboardMarkup:
    """Next is always first — the loop should never need a second tap to continue."""
    kb = InlineKeyboardBuilder()
    kb.button(text=t(lang, "btn_next"), callback_data=NextQuestion(exclude=question_id))
    if locked:
        kb.button(text=t(lang, "btn_unlock"), callback_data=Simple(action="unlock"))
    if explained:
        kb.button(text=t(lang, "btn_report"), callback_data=ReportBad(qid=question_id))
    kb.adjust(1)
    return kb.as_markup()


def settings_menu(lang: str, translations_on: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
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
