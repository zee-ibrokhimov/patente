"""Message text for the companion bot.

Question, verdict and explanation rendering moved to the Mini App along with the
study loop — the rules about keeping the ministerial Italian primary and showing a
paywall only for `locked` (never for `unavailable`) now live in webapp/src/.

What stays here is everything the bot still owns: progress, subscription and
settings.
"""

from __future__ import annotations

import html
import math

from bot.i18n import t
from shared.constants import (
    TIER_DAYS,
    TIER_FEATURED,
    TIER_PRICE_CENTS,
    TRANSLATION_LANGUAGES,
)

# Telegram's photo-caption limit. Statements run ~120 characters and explanations
# two sentences, so this only ever bites on a pathological row — but silently
# losing the end of an explanation would be worse than an ellipsis.
CAPTION_LIMIT = 1024
MESSAGE_LIMIT = 4096


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def stats(data: dict, lang: str, *, top: int = 5) -> str:
    if not data["answers_given"]:
        return t(lang, "stats_empty")

    rate = round(data["error_rate"] * 100)
    lines = [
        t(lang, "stats_title"),
        "",
        t(lang, "stats_summary",
          seen=data["questions_seen"], total=data["questions_total"],
          given=data["answers_given"], wrong=data["wrong"], rate=rate),
    ]

    weak = [topic for topic in data["by_topic"] if topic["wrong"]][:top]
    if weak:
        lines += ["", t(lang, "stats_topics")]
        for topic in weak:
            name = topic["topic"]
            # Ministerial topic names run to 250 characters; the first clause is
            # the recognisable part.
            short = name.split(";")[0].split(" - ")[0].strip()
            lines.append(t(lang, "stats_topic_line",
                           rate=round(topic["error_rate"] * 100),
                           topic=html.escape(_clip(short, 60))))
    return _clip("\n".join(lines), MESSAGE_LIMIT)


def settings(user: dict, lang: str) -> str:
    """Report the translation setting only where it can be true.

    For a language we do not translate into, printing "Translations: on" is a lie the
    settings screen tells about itself.
    """
    from bot.i18n import LANGUAGE_NAMES

    lines = [
        t(lang, "settings_title"),
        t(lang, "settings_language", lang=LANGUAGE_NAMES.get(user["lang"], user["lang"])),
    ]
    if user["lang"] in TRANSLATION_LANGUAGES:
        state = t(lang, "state_on" if user["translations_on"] else "state_off")
        lines.append(t(lang, "settings_translations", state=state))
    return "\n\n".join(lines)


def _money(cents: int) -> str:
    """EUR with two decimals. No locale formatting: the price is the same number in every
    language, and a comma-vs-dot difference between the bot and the checkout page reads
    as two different prices."""
    return f"\u20ac{cents / 100:.2f}"


def selling(user: dict, *, can_subscribe: bool) -> bool:
    """Whether to put a BUY button in front of this person.

    ONE decision, used by both the message and the keyboard, because they were made
    separately and disagreed.

    `can_subscribe` only ever meant "a checkout link is configured" — a deployment fact,
    nothing about the user. So `keyboards.plan_actions` attached "Subscribe" underneath
    every /plan, including the trial message whose entire text is *"your subscription will
    renew automatically, cancel in @tribute"*. Message says do not buy, button says buy,
    and the button wins because it is the only tappable thing on screen. The same button
    sat under a channel-Premium subscriber being shown the free-tier price list.

    `premium` is the single question every paid feature asks, and it is already in the
    payload (api/routes/users.py:78). Someone Premium by ANY route — a pass, channel
    membership, staff — is not a person to sell to. A trialist has `has_pass` and is
    therefore premium, so they are covered by the same test.
    """
    return can_subscribe and not user.get("premium", user.get("has_pass", False))


def plan(user: dict, lang: str, *, can_subscribe: bool) -> str:
    """Subscription state, in the surface that owns payment.

    Plan §6.2 keeps buying in chat rather than in the Mini App, since Mini Apps selling
    digital goods sit closer to Telegram's Stars-only rule and to Apple's review
    guidelines. So every paywall in the app ends here, and this message has to answer the
    question the user arrived with: what do I get, and what does it cost.

    Three states, and they need different messages:
      · on the free trial — say what they have and when it ends, and do NOT sell
      · paid — say until when, and stop selling
      · free — the pitch and the prices
    """
    expires = user.get("pass_expires_at")
    has_pass = bool(user.get("has_pass"))
    purchased = bool(user.get("purchased"))
    lines = [t(lang, "plan_title"), ""]

    # PREMIUM WITHOUT A PASS ROW — channel membership, or staff.
    #
    # This read `has_pass` only, so the other two of the three Premium sources fell
    # straight through to the free-tier pitch: someone whose subscription is real, whose
    # translations and explanations and vocabulary all work, opened the surface that owns
    # payment and was told "Free plan — translations and explanations are Premium",
    # followed by three prices and a Subscribe button.
    #
    # That population is not hypothetical. Tribute adds buyers to the channel itself, and
    # on 2026-07-31 its webhooks were refused for three hours while it did exactly that —
    # so "in the channel, no pass row" is a state this product has already produced. It
    # also includes every administrator and the owner.
    if not has_pass and user.get("premium"):
        via = user.get("premium_via")
        lines.append(t(lang, "plan_active_staff" if via == "staff" else "plan_active_channel"))
        lines += ["", t(lang, "plan_perks")]
        return _clip("\n".join(lines), MESSAGE_LIMIT)

    if has_pass and not purchased and expires:
        # A pass with no MONEY behind it. Two very different things land here and they
        # need opposite sentences:
        #
        #   · a Tribute trial — a card is linked and it WILL be charged unless cancelled
        #   · a hand-granted pass from /grant — no card exists, nothing can be charged
        #
        # `trialing` is what the API says about the first. Telling a trialist "nothing
        # will be charged" is how a chargeback starts; telling a gifted user to go and
        # cancel a subscription they never had is merely confusing, so the ambiguous
        # case defaults to the honest warning.
        days = _days_left(expires)
        lines.append(t(lang, "plan_trial", n=days))
        note = "plan_trial_note" if user.get("trialing", True) else "plan_granted_note"
        lines += ["", t(lang, note)]
        return _clip("\n".join(lines), MESSAGE_LIMIT)

    if has_pass and expires:
        lines.append(t(lang, "plan_active", date=str(expires)[:10]))
        lines += ["", t(lang, "plan_perks")]
        return _clip("\n".join(lines), MESSAGE_LIMIT)

    lines += [t(lang, "plan_free_title"), t(lang, "plan_free_body"), ""]
    lines += [t(lang, "plan_premium_h")]
    for key in ("plan_b_ai", "plan_b_lang", "plan_b_vocab", "plan_b_future"):
        lines.append(t(lang, key))

    lines += ["", t(lang, "plan_prices_h")]
    # Cheapest-per-month last and starred, which is the order plan §4.2 asks for: the
    # spread is visible but not obvious, so presentation does the work.
    for tier in sorted(TIER_DAYS, key=lambda x: TIER_DAYS[x]):
        cents = TIER_PRICE_CENTS[tier]
        months = TIER_DAYS[tier] / 30
        key = "plan_price_best" if tier == TIER_FEATURED else "plan_price_line"
        lines.append(t(
            lang, key,
            label=t(lang, f"plan_{tier.replace('pass_', '')}"),
            price=_money(cents),
            per_month=_money(round(cents / months)),
        ))

    if not can_subscribe:
        lines += ["", t(lang, "payments_not_live")]
    return _clip("\n".join(lines), MESSAGE_LIMIT)


def _days_left(expires: str) -> int:
    """Whole days, rounded UP and floored at 1: a trial with eleven hours left is 'one
    day', not 'zero days', which would read as already over."""
    from datetime import datetime, timezone

    end = datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    seconds = (end - datetime.now(timezone.utc)).total_seconds()
    return max(1, math.ceil(seconds / 86400)) if seconds > 0 else 0
