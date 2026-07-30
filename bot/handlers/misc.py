"""Settings, help, privacy, support and GDPR erasure."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from bot import keyboards, render
from bot.api_client import ApiClient, ApiError
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


@router.message(Command("grant"))
async def grant(message: Message, lang: str, api: ApiClient):
    """`/grant [days] [chat_id]` — give yourself or a tester a pass by hand.

    Plan §12 wanted this for the first missed Tribute webhook. It is needed before that
    too: translations and explanations are both paid, so without it the only way to see
    the product working is editing SQLite by hand.

    Silent for non-admins rather than refusing. A stranger who guesses the command should
    learn nothing from it, and there is no legitimate user to explain the refusal to.
    """
    if message.from_user.id not in config.admin_ids:
        return

    parts = (message.text or "").split()
    days = int(parts[1]) if len(parts) > 1 and parts[1].lstrip("-").isdigit() else 30
    target = int(parts[2]) if len(parts) > 2 and parts[2].lstrip("-").isdigit() else message.from_user.id
    if days <= 0:
        await message.answer("days must be positive")
        return

    try:
        updated = await api.grant_pass(target, days, reason=f"/grant by {message.from_user.id}")
    except ApiError as exc:
        await message.answer(f"could not grant: {exc.detail}")
        return
    await message.answer(
        f"pass for {target} now runs to {updated['pass_expires_at']}"
    )


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
