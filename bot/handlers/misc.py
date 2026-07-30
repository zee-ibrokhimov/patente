"""Settings, help, privacy, support and GDPR erasure."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from bot import keyboards, render
from bot.api_client import ApiClient, ApiError
from bot.i18n import t
from shared.config import settings as config

log = logging.getLogger(__name__)

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
    """All three locales say "open it with the button below". They were telling the
    truth about a button that was not attached — and since drilling moved to the Mini
    App, that hand-off is the single most important affordance the bot has."""
    await message.answer(
        t(lang, "help", disclaimer=t(lang, "disclaimer")),
        reply_markup=keyboards.open_app(lang),
    )


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


async def _replace(query: CallbackQuery, text: str) -> None:
    """Edit the tapped message, or say it another way if that is impossible.

    `query.message` may be an InaccessibleMessage — deleted, or older than a bot is
    allowed to edit — and that type has no `edit_text`. An unguarded edit therefore
    raises AFTER whatever the handler already did has committed, and the generic error
    handler then tells the user it failed. For /delete that is the worst possible
    ordering: the erasure succeeded, the user is told it did not, and the rational
    response is to try again.
    """
    try:
        await query.message.edit_text(text, reply_markup=None)
    except Exception:
        log.warning("could not edit the tapped message; answering instead", exc_info=True)
        try:
            await query.bot.send_message(query.from_user.id, text)
        except Exception:
            log.warning("could not message the user either", exc_info=True)


@router.callback_query(F.data == "s:delete_yes")
async def delete_confirmed(query: CallbackQuery, lang: str, api: ApiClient):
    """Erasure is irreversible, so confirm it happened even if the screen cannot be
    updated. The answer() comes first: it is what stops the button spinning, and it is
    the one acknowledgement that does not depend on the message still existing."""
    await api.delete_user(query.from_user.id)
    await query.answer()
    await _replace(query, t(lang, "delete_done"))


@router.callback_query(F.data == "s:delete_no")
async def delete_cancelled(query: CallbackQuery, lang: str):
    await query.answer()
    await _replace(query, t(lang, "delete_cancelled"))
