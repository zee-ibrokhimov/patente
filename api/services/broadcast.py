"""Sending a message to many learners at once.

The owner grants access by hand now, so they need to reach people: a newsletter to everyone,
and a private reply to the person who just asked to pay.

WHY THIS IS NOT A LOOP OVER notify.send

Telegram's limit is roughly 30 messages per second across a bot, and exceeding it earns a
429 with a `retry_after` — not a dropped message, a THROTTLED BOT. A newsletter that trips
it degrades every other thing the bot does, including the /start of someone who just
arrived through a paid referral link.

So: a fixed pace, and a 429 is obeyed rather than retried blindly.

WHY IT RUNS DETACHED

Five hundred users at the pace below is most of a minute. A request that waits for it would
hold a connection and time out at the proxy long before it finished, and the caller would
have no idea whether the send had happened. It runs as a background task and the result goes
to the event log, which is also the only record that a newsletter was ever sent.

WHO IS EXCLUDED, ALWAYS

Someone who has deleted their account. `users` is the list, and a deletion removes the row,
so there is no separate suppression list to keep in step — which is the kind of thing that
goes wrong quietly and mails somebody who asked not to be mailed.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.models import Event, User
from api.services import events, notify
from shared.config import settings
from shared.constants import EV_BROADCAST_SENT
from shared.db import async_session_factory

log = logging.getLogger(__name__)

# Messages per second. Telegram's ceiling is about 30; this leaves headroom so a newsletter
# never becomes the reason a learner's /start is throttled.
PER_SECOND = 20
PAUSE = 1.0 / PER_SECOND

# A newsletter to more than this many people is almost certainly a mistake at this stage of
# the product, and the mistake is unrecoverable — you cannot unsend. Raise it deliberately.
MAX_RECIPIENTS = 5_000


async def recipients(
    session: AsyncSession, *, lang: str | None = None, premium_only: bool = False
) -> list[int]:
    """Who a newsletter would reach, in a stable order.

    `lang` targets one language, because a message written in Russian is noise to an
    Italian reader — and noise is what makes people mute a bot they were paying for.
    """
    stmt = select(User.chat_id).order_by(User.chat_id)
    if lang:
        stmt = stmt.where(User.lang == lang)
    if premium_only:
        stmt = stmt.where(User.pass_expires_at > datetime.now(timezone.utc))
    return list(await session.scalars(stmt))


MAX_BUTTONS = 3


def buttons_for(specs: list[dict] | None) -> list[dict]:
    """Validate newsletter buttons into Telegram's shape, or refuse them.

    Three kinds and nothing else:

      · {"text": ..., "webapp": true}  — opens the Mini App INSIDE Telegram. This is the
        one that matters for an offer: it lands on the app in a single tap instead of
        bouncing the reader into a browser, where they are simply gone.
      · {"text": ..., "url": "https://..."}
      · {"text": ..., "chat": "@handle"} — a t.me link, for "message me to pay".

    An inline button is a place thousands of people are being sent at once, so the URL is
    checked rather than trusted: https only. `javascript:` and `tg://` are both rendered
    happily by Telegram, and the second can act on the reader's own account.
    """
    if not specs:
        return []
    if len(specs) > MAX_BUTTONS:
        raise ValueError(f"at most {MAX_BUTTONS} buttons")

    out: list[dict] = []
    for spec in specs:
        label = (spec.get("text") or "").strip()
        if not label:
            raise ValueError("every button needs a label")
        if len(label) > 64:
            raise ValueError(f"button label too long: {label[:32]}…")

        if spec.get("webapp"):
            if not settings.webapp_url:
                raise ValueError(
                    "WEBAPP_URL is not configured, so a Mini App button would open nothing")
            out.append({"text": label, "web_app": {"url": settings.webapp_url}})
            continue

        handle = (spec.get("chat") or "").lstrip("@").strip()
        if handle:
            out.append({"text": label, "url": f"https://t.me/{handle}"})
            continue

        url = (spec.get("url") or "").strip()
        if not url.startswith("https://"):
            raise ValueError(f"button {label!r} needs an https:// url, a chat, or webapp")
        out.append({"text": label, "url": url})
    return out


async def send_many(chat_ids: list[int], text: str, *, sent_by: int,
                    label: str = "", photo_url: str | None = None,
                    buttons: list[dict] | None = None) -> dict:
    """Send `text` to each id at a fixed pace. Never raises.

    Returns counts. A failure is normal rather than exceptional — someone who blocked the
    bot, or who was granted access and then deleted their account — so the return value is
    a tally, not an exception.
    """
    delivered = failed = 0
    for chat_id in chat_ids[:MAX_RECIPIENTS]:
        try:
            ok = await notify.send_rich(chat_id, text,
                                        photo_url=photo_url, buttons=buttons)
        except Exception:                                             # noqa: BLE001
            # notify.send already swallows the normal failures; this is the belt for
            # anything it does not, because one bad id must not end the newsletter.
            log.warning("broadcast to %s raised", chat_id, exc_info=True)
            ok = False
        delivered += 1 if ok else 0
        failed += 0 if ok else 1
        await asyncio.sleep(PAUSE)

    factory = async_session_factory()
    async with factory() as session:
        await events.record(
            session, EV_BROADCAST_SENT, chat_id=sent_by,
            recipients=len(chat_ids), delivered=delivered, failed=failed,
            label=label[:80], preview=text[:200],
        )
        await session.commit()

    log.info("broadcast %r: %s delivered, %s failed", label, delivered, failed)
    return {"recipients": len(chat_ids), "delivered": delivered, "failed": failed}


async def history(session: AsyncSession, limit: int = 20) -> list[dict]:
    """What has been sent, so the owner can see it was sent and to how many.

    From the event log rather than a table of its own: it is append-only, it is already
    backed up, and a newsletter is exactly the kind of thing somebody needs to check they
    did not send twice.
    """
    rows = await session.scalars(
        select(Event).where(Event.type == EV_BROADCAST_SENT)
        .order_by(Event.created_at.desc()).limit(limit)
    )
    return [
        {
            "at": row.created_at,
            "recipients": (row.payload or {}).get("recipients", 0),
            "delivered": (row.payload or {}).get("delivered", 0),
            "failed": (row.payload or {}).get("failed", 0),
            "label": (row.payload or {}).get("label", ""),
            "preview": (row.payload or {}).get("preview", ""),
        }
        for row in rows
    ]
