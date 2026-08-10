"""Telling a user what just happened to their money.

The webhook is the only place the app learns that a payment occurred, and until now it
was silent: a trial started, a card was linked, and the person who did it received
nothing at all. For an ordinary purchase that is merely rude. For a trial that will
charge a card in seven days it is worse than rude — under EU distance-selling rules the
terms of a recurring charge have to be communicated, and "we told you on the checkout
page" is a weak position when the app itself said nothing.

WHY THE API SENDS THIS AND NOT THE BOT

Plan §6.1 keeps the bot free of business logic and gives the API the database. A payment
arrives at the API, so the API is the only process that knows about it. Handing it to the
bot would need a queue, a poll or an internal endpoint — three moving parts to deliver one
message. The Bot API is a plain HTTPS call and the token is already in this container's
environment.

BEST EFFORT, ALWAYS

Nothing here may fail a webhook. Tribute retries a non-2xx, and a retry cannot fix
Telegram being slow — it would just redeliver a payment that was already applied. Every
failure is caught and logged, and the money side of the transaction is committed before
this is ever called.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx

from shared.config import settings
from shared.constants import TIER_PRICE_CENTS

log = logging.getLogger(__name__)

TIMEOUT = 10.0

# Telegram's caption limit. A message is 4096; a photo caption is 1024, and the API does not
# wrap or warn — it rejects, or the client shows a cut-off caption. See send_rich.
PHOTO_CAPTION_LIMIT = 1024


def _money(cents: int) -> str:
    """Formatted without locale rules, deliberately: the same number must appear in the
    bot, on Tribute's checkout page and on the card statement. A comma here and a dot
    there reads as two different prices."""
    return f"€{cents // 100}.{cents % 100:02d}"


def _date(when: datetime | None) -> str:
    return when.strftime("%d.%m.%Y") if when else "—"


# One message per event, per language. Written out rather than assembled from fragments:
# these are the only messages in the product that talk about someone's money, and a
# sentence stitched from four keys is how a translation ends up subtly promising the
# wrong thing.
MESSAGES: dict[str, dict[str, str]] = {
    "trial": {
        "ru": ("🎁 <b>Пробный период активирован</b>\n\n"
               "У вас есть полный доступ до <b>{date}</b>.\n\n"
               "После этой даты автоматически спишется {price} за подписку. "
               "Отменить можно в любой момент до {date} — тогда с вас ничего не спишут.\n\n"
               "Управлять подпиской: @tribute"),
        "en": ("🎁 <b>Free trial started</b>\n\n"
               "You have full access until <b>{date}</b>.\n\n"
               "After that date {price} will be charged automatically. "
               "You can cancel any time before {date} and you will not be charged.\n\n"
               "Manage your subscription: @tribute"),
        "it": ("🎁 <b>Prova gratuita attivata</b>\n\n"
               "Hai accesso completo fino al <b>{date}</b>.\n\n"
               "Dopo tale data verranno addebitati {price} automaticamente. "
               "Puoi disdire in qualsiasi momento prima del {date} senza alcun addebito.\n\n"
               "Gestisci l'abbonamento: @tribute"),
        "uz": ("🎁 <b>Sinov muddati boshlandi</b>\n\n"
               "<b>{date}</b> gacha to'liq kirish huquqiga egasiz.\n\n"
               "Shu sanadan keyin {price} avtomatik yechib olinadi. "
               "{date} gacha istalgan vaqtda bekor qilsangiz, hech narsa yechilmaydi.\n\n"
               "Obunani boshqarish: @tribute"),
    },
    "paid": {
        "ru": ("✅ <b>Оплата получена</b>\n\n"
               "Premium активен до <b>{date}</b>.\n\n"
               "Спасибо! Управлять подпиской: @tribute"),
        "en": ("✅ <b>Payment received</b>\n\n"
               "Premium is active until <b>{date}</b>.\n\n"
               "Thank you. Manage your subscription: @tribute"),
        "it": ("✅ <b>Pagamento ricevuto</b>\n\n"
               "Premium è attivo fino al <b>{date}</b>.\n\n"
               "Grazie. Gestisci l'abbonamento: @tribute"),
        "uz": ("✅ <b>To'lov qabul qilindi</b>\n\n"
               "Premium <b>{date}</b> gacha faol.\n\n"
               "Rahmat! Obunani boshqarish: @tribute"),
    },
    # Sent when the subscription actually terminates rather than when the user asks —
    # observed with Tribute, which registers "cancels on 07.08" immediately and only
    # emits the event on that date. So by the time this arrives the access is usually
    # already gone, and "you keep access until 07.08" on 07.08 tells the reader nothing.
    "ended": {
        "ru": ("Подписка завершена.\n\n"
               "Premium-доступ закончился. Все 7106 вопросов, экзамен и тренировка "
               "остаются бесплатными.\n\n"
               "Вернуться в Premium: /plan"),
        "en": ("Subscription ended.\n\n"
               "Premium access has finished. All 7106 questions, exam mode and practice "
               "remain free.\n\n"
               "Come back to Premium: /plan"),
        "it": ("Abbonamento terminato.\n\n"
               "L'accesso Premium è finito. Tutte le 7106 domande, l'esame e "
               "l'esercitazione restano gratuiti.\n\n"
               "Torna a Premium: /plan"),
        "uz": ("Obuna tugadi.\n\n"
               "Premium kirish yakunlandi. Barcha 7106 ta savol, imtihon va mashq "
               "bepul qoladi.\n\n"
               "Premiumga qaytish: /plan"),
    },
    "ending": {
        "ru": ("⏳ <b>Premium заканчивается через {days} дн.</b>\n\n"
               "Доступ действует до <b>{date}</b>. После этого вопросы, экзамен и "
               "тренировка останутся бесплатными, а объяснения, переводы и словарь "
               "отключатся.\n\nПродлить: /plan"),
        "en": ("⏳ <b>Premium ends in {days} days</b>\n\n"
               "You have access until <b>{date}</b>. After that the questions, the exam "
               "and practice stay free; explanations, translations and the vocabulary "
               "switch off.\n\nRenew: /plan"),
        "it": ("⏳ <b>Premium finisce tra {days} giorni</b>\n\n"
               "Hai accesso fino al <b>{date}</b>. Dopo, domande, esame ed esercitazione "
               "restano gratuiti; spiegazioni, traduzioni e vocabolario si disattivano."
               "\n\nRinnova: /plan"),
        "uz": ("⏳ <b>Premium {days} kundan keyin tugaydi</b>\n\n"
               "<b>{date}</b> gacha kirish bor. Keyin savollar, imtihon va mashq bepul "
               "qoladi; izohlar, tarjimalar va lug'at o'chadi.\n\nUzaytirish: /plan"),
    },
    "lapsed": {
        "ru": ("Premium закончился <b>{date}</b>.\n\n"
               "Все 7106 вопросов, экзамен и тренировка по-прежнему бесплатны. "
               "Объяснения, переводы и словарь снова появятся с подпиской.\n\n/plan"),
        "en": ("Premium ended on <b>{date}</b>.\n\n"
               "All 7106 questions, the exam and practice are still free. Explanations, "
               "translations and the vocabulary come back with a subscription.\n\n/plan"),
        "it": ("Premium è finito il <b>{date}</b>.\n\n"
               "Tutte le 7106 domande, l'esame e l'esercitazione restano gratuiti. "
               "Spiegazioni, traduzioni e vocabolario tornano con l'abbonamento.\n\n/plan"),
        "uz": ("Premium <b>{date}</b> da tugadi.\n\n"
               "Barcha 7106 ta savol, imtihon va mashq hamon bepul. Izohlar, tarjimalar "
               "va lug'at obuna bilan qaytadi.\n\n/plan"),
    },
    "cancelled": {
        "ru": ("Подписка отменена.\n\n"
               "Доступ сохраняется до <b>{date}</b> — вы уже оплатили этот период. "
               "После этой даты списаний не будет.\n\n"
               "Передумали? /plan"),
        "en": ("Subscription cancelled.\n\n"
               "You keep access until <b>{date}</b> — that period is already paid for. "
               "Nothing further will be charged.\n\n"
               "Changed your mind? /plan"),
        "it": ("Abbonamento disdetto.\n\n"
               "Mantieni l'accesso fino al <b>{date}</b> — quel periodo è già pagato. "
               "Non verrà addebitato altro.\n\n"
               "Cambiato idea? /plan"),
        "uz": ("Obuna bekor qilindi.\n\n"
               "<b>{date}</b> gacha kirish saqlanadi — bu davr allaqachon to'langan. "
               "Boshqa hech narsa yechilmaydi.\n\n"
               "Fikringiz o'zgardimi? /plan"),
    },
}


# What to write where a price belongs.
#
# It used to be TIER_PRICE_CENTS[tier]. On a TRIAL that is always wrong: Tribute sends
# `period: "trial"`, which has no tier of its own and falls back to the shortest one — so
# every trial message quoted EUR 2.99 no matter which plan was bought. Someone who chose
# six months was told in writing they would be charged 2.99 and would then be charged
# 10.99. A number we cannot source is worse than no number, because a wrong one in
# writing is what a chargeback is argued from.
PRICE_UNKNOWN = {
    "ru": "цена выбранного плана",
    "en": "the price of the plan you chose",
    "it": "il prezzo del piano scelto",
    "uz": "siz tanlagan reja narxi",
}


def _price_words(tier: str, lang: str = "en") -> str:
    """The price, or an honest phrase when the event does not carry one."""
    cents = TIER_PRICE_CENTS.get(tier)
    if not cents:
        return PRICE_UNKNOWN.get(lang, PRICE_UNKNOWN["en"])
    return _money(cents)


def compose(kind: str, lang: str, expires_at: datetime | None, tier: str,
            now: datetime | None = None, days: int = 0) -> str:
    """The message for this event in this language, falling back to English.

    A language we have not written yet must produce a real sentence, not a KeyError and
    not the string "None" — this is the message that explains a charge.

    A cancellation whose final date has already passed becomes "ended" instead. Tribute
    emits the event when the subscription terminates, not when the user asks for it, so
    the common case is that access is already over — and telling someone they keep access
    until a date that is today is worse than saying nothing.
    """
    if kind == "cancelled" and expires_at is not None:
        moment = now or datetime.now(timezone.utc)
        if expires_at <= moment:
            kind = "ended"
    table = MESSAGES[kind]
    template = table.get(lang) or table["en"]
    # On a trial the tier is a fallback, never the plan the buyer actually chose, so no
    # figure is quoted at all. On a real payment the tier came from Tribute's `period`
    # and is trustworthy.
    price = PRICE_UNKNOWN.get(lang, PRICE_UNKNOWN["en"]) if kind == "trial" \
        else _price_words(tier, lang)
    return template.format(date=_date(expires_at), price=price, days=days)


async def send(chat_id: int, text: str) -> bool:
    """Send one message. Returns whether it went; never raises.

    A user who has blocked the bot, or who bought before ever opening it, is a 403 from
    Telegram — a normal outcome for a payment webhook, not an error worth alarming about.
    """
    token = settings.bot_token
    if not token:
        log.warning("no bot token configured — cannot tell %s about their payment", chat_id)
        return False
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            r = await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                      "disable_web_page_preview": True},
            )
        if r.status_code == 200:
            return True
        # Logged at INFO for the expected refusals and WARNING for anything else, so a
        # blocked bot does not look like a broken integration.
        level = log.info if r.status_code in (400, 403) else log.warning
        level("could not message %s about their payment: %s %s",
              chat_id, r.status_code, r.text[:200])
        return False
    except Exception as exc:  # noqa: BLE001 — a webhook must never fail on this
        log.warning("could not message %s about their payment: %s", chat_id, exc)
        return False


async def send_rich(
    chat_id: int,
    text: str,
    *,
    photo_url: str | None = None,
    buttons: list[dict] | None = None,
) -> bool:
    """One newsletter message: optional photo, optional inline keyboard. Never raises.

    Two Telegram methods rather than one, because a photo CAPTION is capped at 1024
    characters against 4096 for a plain message — so a long newsletter sent as a caption
    silently loses its ending. Over the cap the photo is dropped and the text goes whole:
    the words are the point, and a truncated last paragraph is worse than no picture.

    `buttons` arrive already validated into Telegram's shape. A web_app button opens the
    Mini App INSIDE Telegram, which is what makes "here is an offer" land on the paywall in
    one tap instead of bouncing someone into a browser and losing them.
    """
    token = settings.bot_token
    if not token:
        log.warning("no bot token configured — cannot reach %s", chat_id)
        return False

    markup = {"inline_keyboard": [[b] for b in buttons]} if buttons else None
    use_photo = bool(photo_url) and len(text) <= PHOTO_CAPTION_LIMIT

    if use_photo:
        method = "sendPhoto"
        payload: dict = {"chat_id": chat_id, "photo": photo_url, "caption": text,
                         "parse_mode": "HTML"}
    else:
        method = "sendMessage"
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                   "disable_web_page_preview": False}
    if markup:
        payload["reply_markup"] = markup

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            r = await client.post(f"https://api.telegram.org/bot{token}/{method}",
                                  json=payload)
        if r.status_code == 200:
            return True
        level = log.info if r.status_code in (400, 403) else log.warning
        level("could not send newsletter to %s: %s %s",
              chat_id, r.status_code, r.text[:200])
        return False
    except Exception as exc:  # noqa: BLE001 — one recipient must not end a newsletter
        log.warning("could not send newsletter to %s: %s", chat_id, exc)
        return False


async def reminder(chat_id: int, lang: str) -> bool:
    """A come-back nudge, with a way in and a way out.

    Both buttons matter. Open is the point — a reminder that makes somebody hunt for the
    app has spent its one chance on friction. Stop is what makes the whole feature
    defensible: without it the only way to end an unwanted message is to block the bot,
    which also ends the payment notices and the renewal warning.

    The text lives in the BOT's locale files, which the API does not load, so it is passed
    through the same tiny reader the bot uses rather than duplicated here.
    """
    buttons: list[dict] = []
    if settings.webapp_url.startswith("https://"):
        buttons.append({"text": _bot_text(lang, "open_app"),
                        "web_app": {"url": settings.webapp_url}})
    buttons.append({"text": _bot_text(lang, "reminder_stop"),
                    "callback_data": "r:stop"})
    return await send_rich(chat_id, _bot_text(lang, "reminder"), buttons=buttons)


async def streak_at_risk(chat_id: int, lang: str, days: int, left: int) -> bool:
    """"Your {days}-day streak needs {left} more questions today."

    Carries the two numbers because a nudge that does not say how close you are is a nudge
    that cannot be acted on without opening the app to find out — and the reason this
    message exists at all is that people do not realise the day is unbanked.

    Same two buttons as the come-back reminder, and the stop button matters more here: this
    is the message someone will mute the bot over if it outstays its welcome, and muting the
    bot also silences the renewal warning.
    """
    buttons: list[dict] = []
    if settings.webapp_url.startswith("https://"):
        buttons.append({"text": _bot_text(lang, "open_app"),
                        "web_app": {"url": settings.webapp_url}})
    buttons.append({"text": _bot_text(lang, "reminder_stop"),
                    "callback_data": "r:stop"})
    text = _bot_text(lang, "streak_at_risk").format(days=days, left=left)
    return await send_rich(chat_id, text, buttons=buttons)


def _bot_text(lang: str, key: str) -> str:
    """One string from the bot's locale files.

    The API deliberately does not import from `bot/` — they are separate processes with
    separate deployments — so this reads the JSON directly. Falls back to Russian, then to
    the key, which is exactly what the bot's own reader does: a missing string must never
    render as "None" at a user.
    """
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "bot" / "locales"
    for candidate in (lang, "ru"):
        path = root / f"{candidate}.json"
        if path.exists():
            value = json.loads(path.read_text(encoding="utf-8")).get(key)
            if value:
                return value
    return key


async def payment(chat_id: int, lang: str, kind: str, expires_at: datetime | None,
                  tier: str, days: int = 0) -> bool:
    """Tell someone what just happened to their subscription."""
    return await send(chat_id, compose(kind, lang, expires_at, tier, days=days))
