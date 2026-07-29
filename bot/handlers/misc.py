"""Settings, help, privacy, support and GDPR erasure."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from bot import keyboards, render
from bot.api_client import ApiClient
from bot.i18n import t
from shared.config import settings as config

router = Router(name="misc")


@router.message(Command("settings"))
async def show_settings(message: Message, user: dict, lang: str):
    await message.answer(
        render.settings(user, lang),
        reply_markup=keyboards.settings_menu(lang, user["translations_on"]),
    )


@router.callback_query(F.data == "s:toggle_translations")
async def toggle_translations(query: CallbackQuery, user: dict, lang: str, api: ApiClient):
    updated = await api.update_user(
        query.from_user.id, translations_on=not user["translations_on"]
    )
    await query.message.edit_text(
        render.settings(updated, lang),
        reply_markup=keyboards.settings_menu(lang, updated["translations_on"]),
    )
    await query.answer()


@router.message(Command("help"))
async def help_command(message: Message, lang: str):
    await message.answer(t(lang, "help", disclaimer=t(lang, "disclaimer")))


@router.message(Command("privacy"))
async def privacy(message: Message, lang: str):
    await message.answer(t(lang, "privacy"))


@router.message(Command("support"))
async def support(message: Message, lang: str):
    contact = config.support_contact or "/help"
    await message.answer(t(lang, "support", contact=contact))


@router.message(Command("delete"))
async def delete_prompt(message: Message, lang: str):
    """Erasure is irreversible, so it asks first — but only once."""
    await message.answer(t(lang, "delete_confirm"), reply_markup=keyboards.confirm_delete(lang))


@router.callback_query(F.data == "s:delete_yes")
async def delete_confirmed(query: CallbackQuery, lang: str, api: ApiClient):
    await api.delete_user(query.from_user.id)
    await query.message.edit_text(t(lang, "delete_done"), reply_markup=None)
    await query.answer()


@router.callback_query(F.data == "s:delete_no")
async def delete_cancelled(query: CallbackQuery, lang: str):
    await query.message.edit_text(t(lang, "delete_cancelled"), reply_markup=None)
    await query.answer()
