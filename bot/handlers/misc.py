"""Settings, help, privacy, support and GDPR erasure."""

from __future__ import annotations

import pathlib

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


def _money(cents: int) -> str:
    return f"€{cents // 100}.{cents % 100:02d}"


def _when(value) -> str:
    """Dates arrive from the API as ISO strings. Shown to one person on a phone, so the
    date alone is enough — the time is noise in every case this is read for."""
    if not value:
        return "—"
    return str(value)[:10]


@router.message(Command("admin"))
async def admin_overview(message: Message, lang: str, api: ApiClient):
    """The state of the business, for the owner.

    Silent for everyone else, matching /grant: a stranger who guesses the command should
    learn nothing from it, and there is no legitimate user to explain a refusal to.
    """
    if message.from_user.id not in config.admin_ids:
        return
    try:
        d = await api.admin_overview()
    except ApiError as exc:
        await message.answer(f"could not load: {exc.detail}")
        return

    lines = [
        "<b>Overview</b>",
        "",
        f"👥 users <b>{d['users']}</b>  (+{d['users_new_week']} this week)",
        f"🟢 active today <b>{d['active_day']}</b>",
        "",
        "<b>Premium</b>",
        f"  pass in our database  <b>{d['with_pass']}</b>",
        f"  in the channel        <b>{d['in_channel']}</b>",
        "",
        "<b>Money</b>",
        f"  paid purchases  <b>{d['purchases']}</b>",
        f"  revenue         <b>{_money(d['revenue_cents'])}</b>",
        f"  refunded        <b>{d['refunded']}</b>",
        "",
        "<b>This week</b>",
        f"  trials started   <b>{d['trials_week']}</b>",
        f"  conversions      <b>{d['conversions_week']}</b>",
        f"  paywall hits     <b>{d['paywall_hits_week']}</b>",
        f"  exams sat        <b>{d['exams_week']}</b>",
    ]
    if d.get("sources"):
        lines += ["", "<b>Where they came from</b>"]
        for row in d["sources"]:
            lines.append(f"  {row['source']}  <b>{row['users']}</b>")
    await message.answer("\n".join(lines))


@router.message(Command("whois"))
async def admin_whois(message: Message, lang: str, api: ApiClient):
    """`/whois <chat_id>` — everything about one person.

    Built for the one support message a payment integration guarantees: "I paid and I
    have no access". On 2026-07-31 that had a real cause — webhooks were refused for
    three hours — and answering it meant opening the database over SSH.
    """
    if message.from_user.id not in config.admin_ids:
        return

    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        await message.answer("usage: /whois &lt;chat_id&gt;")
        return
    target = int(parts[1])

    try:
        d = await api.admin_whois(target)
    except ApiError as exc:
        if exc.status == 404:
            # Worth saying plainly: the purchase webhook CREATES a user, so no row at all
            # means no payment ever reached us — which is the answer, not a dead end.
            await message.answer(
                f"no user {target}.\n\nA purchase would have created one, so nothing "
                f"from Tribute has ever arrived for this id."
            )
            return
        await message.answer(f"could not load: {exc.detail}")
        return

    lines = [
        f"<b>{d['chat_id']}</b>  ({d['lang']})",
        f"joined {_when(d['created_at'])}",
        "",
        f"pass     <b>{'yes' if d['has_pass'] else 'no'}</b>  until {_when(d['pass_expires_at'])}",
        f"channel  <b>{d['channel_status'] or 'never checked'}</b>  ({_when(d['channel_checked_at'])})",
        f"free explanations used: {d['free_explanations_used']}",
    ]
    if d["purchases"]:
        lines += ["", "<b>Purchases</b>"]
        for p in d["purchases"][:5]:
            tag = " REFUNDED" if p["refunded_at"] else ""
            kind = "trial" if p["amount_cents"] == 0 else _money(p["amount_cents"])
            lines.append(f"  {kind} {p['tier']} → {_when(p['extended_to'])}{tag}")
    else:
        lines += ["", "no purchases"]
    if d["recent_events"]:
        lines += ["", "<b>Recent</b>"]
        lines += [f"  {e['type']} {_when(e['at'])}" for e in d["recent_events"]]
    await message.answer("\n".join(lines))


@router.message(F.photo)
async def receive_asset(message: Message, lang: str):
    """Admin-only: send the bot a picture and it lands on the server.

    Built because the owner had icons on a Windows desktop and there was no way to get
    them onto the box — file:// paths are not reachable, and pasting a PNG into a chat
    gives something that can be looked at but not saved.

    Telegram compresses `photo`, which is wrong for an icon: it re-encodes to JPEG and
    destroys the alpha channel. So this says so and asks for the file to be sent as a
    DOCUMENT instead, which is delivered byte-for-byte.
    """
    if message.from_user.id not in config.admin_ids:
        return
    await message.answer(
        "Send it as a <b>file</b>, not a photo.\n\n"
        "Telegram re-encodes photos to JPEG and throws away transparency, which ruins an "
        "icon. On desktop: attach → <i>File</i> rather than <i>Photo</i>."
    )


@router.message(F.document)
async def receive_asset_document(message: Message, lang: str):
    """Admin-only: save an uploaded file into the shared volume.

    Restricted to images and to a size a Mini App icon could plausibly be. Not because a
    stranger could reach this — non-admins fall out on the first line — but because the
    owner sending the wrong thing by accident should fail loudly rather than fill a disk
    that took every service on this box down once already.
    """
    if message.from_user.id not in config.admin_ids:
        return

    doc = message.document
    name = (doc.file_name or "upload.bin").replace("/", "_").replace("..", "_")
    if not name.lower().endswith((".png", ".svg", ".jpg", ".jpeg", ".webp")):
        await message.answer(f"{name}: not an image, ignoring.")
        return
    if (doc.file_size or 0) > 5 * 1024 * 1024:
        await message.answer(f"{name}: over 5 MB, ignoring.")
        return

    target = pathlib.Path("/data/uploads")
    target.mkdir(parents=True, exist_ok=True)
    path = target / name
    try:
        await message.bot.download(doc, destination=path)
    except Exception as exc:  # noqa: BLE001 — an upload failing must not kill the bot
        await message.answer(f"could not save {name}: {exc}")
        return
    await message.answer(f"saved <code>{path}</code> ({path.stat().st_size // 1024} KB)")
