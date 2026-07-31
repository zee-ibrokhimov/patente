"""Validate Telegram Mini App `initData`.

This is the only thing standing between the public internet and an API that has no
authentication (plan §6.3). Every existing route takes `chat_id` from the URL path,
so identity there is *claimed* by the caller — safe on loopback, catastrophic in a
browser. The Mini App router does not use those routes: it derives the chat id from
the signature verified here, and never reads it from the request.

The scheme is Telegram's:

    secret_key       = HMAC_SHA256(key="WebAppData", msg=<bot token>)
    data_check_string = "\\n".join(f"{k}={v}" for k, v in sorted(fields) if k != "hash")
    expected          = HMAC_SHA256(key=secret_key, msg=data_check_string).hexdigest()

Note the inversion in the first line — the literal string "WebAppData" is the *key*
and the bot token is the *message*. Doing it the other way round produces a
plausible-looking digest that never matches, and the mistake is easy to stare past.

Three deliberate choices, mirroring api/services/purchases.py:

  · **No token configured refuses everything.** An unvalidated Mini App request that
    returns paid content hands the product to anyone who can guess the URL. Same
    reasoning as TRIBUTE_WEBHOOK_SECRET being unset.
  · **Comparison is constant-time.** hmac.compare_digest, not `==`.
  · **`auth_date` is checked.** A signature stays valid forever otherwise, so one
    captured initData would be a permanent credential for that account.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl

from shared.config import settings

log = logging.getLogger(__name__)

# Telegram issues initData when the Mini App is opened and the client keeps sending
# the same blob for the life of the session, so this cannot be short. A day bounds
# the damage from a leaked blob while leaving a long study session working.
MAX_AGE_SECONDS = 24 * 60 * 60


class InitDataRejected(Exception):
    """The payload is absent, malformed, unsigned, forged or stale."""


@dataclass(frozen=True)
class TelegramUser:
    """The parts of Telegram's `user` object we trust, because they were signed."""

    chat_id: int
    language_code: str | None = None
    username: str | None = None
    # The learner's own first name, for the leaderboard and nothing else. Signed by
    # Telegram like everything else here, so it cannot be spoofed by the client — which
    # matters when it is about to be shown to other people.
    first_name: str | None = None


def _secret_key(bot_token: str) -> bytes:
    return hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()


def verify(init_data: str, *, now: float | None = None) -> TelegramUser:
    """Return the signed-for user, or raise InitDataRejected.

    `init_data` is the raw query string exactly as Telegram handed it to the client.
    It must not be re-encoded on the way here: the HMAC covers these bytes, and a
    round trip through a dict-and-back reorders and re-escapes them.
    """
    if not settings.bot_token:
        raise InitDataRejected("bot token is not configured — refusing all Mini App requests")
    if not init_data:
        raise InitDataRejected("no initData supplied")

    # keep_blank_values: a field Telegram signed as empty still takes part in the hash.
    fields = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=False))
    received = fields.pop("hash", None)
    if not received:
        raise InitDataRejected("initData carries no hash")

    check_string = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    expected = hmac.new(
        _secret_key(settings.bot_token), check_string.encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, received):
        raise InitDataRejected("initData signature does not match")

    raw_auth_date = fields.get("auth_date")
    if not raw_auth_date:
        raise InitDataRejected("initData carries no auth_date")
    try:
        auth_date = int(raw_auth_date)
    except ValueError as exc:
        raise InitDataRejected("initData auth_date is not an integer") from exc

    age = (time.time() if now is None else now) - auth_date
    if age > MAX_AGE_SECONDS:
        raise InitDataRejected(f"initData is stale ({int(age)}s old)")
    # A little clock skew is normal; a large negative age is not, and accepting it
    # would let a forged future auth_date never expire.
    if age < -MAX_AGE_SECONDS:
        raise InitDataRejected("initData auth_date is in the future")

    try:
        user = json.loads(fields["user"])
        chat_id = int(user["id"])
    except (KeyError, ValueError, TypeError) as exc:
        raise InitDataRejected("initData has no usable user object") from exc

    return TelegramUser(
        chat_id=chat_id,
        language_code=user.get("language_code"),
        username=user.get("username"),
        first_name=user.get("first_name"),
    )


def sign(fields: dict[str, str], bot_token: str) -> str:
    """Produce a valid initData string. Test helper, and the only way to prove the
    verifier accepts what Telegram actually sends rather than what we imagine."""
    check_string = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    digest = hmac.new(
        _secret_key(bot_token), check_string.encode(), hashlib.sha256
    ).hexdigest()
    from urllib.parse import urlencode

    return urlencode({**fields, "hash": digest})
