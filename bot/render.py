"""Message text for the companion bot.

Question, verdict and explanation rendering moved to the Mini App along with the
study loop — the rules about keeping the ministerial Italian primary and showing a
paywall only for `locked` (never for `unavailable`) now live in webapp/src/.

What stays here is everything the bot still owns: progress, subscription and
settings.
"""

from __future__ import annotations

import html

from bot.i18n import t
from shared.constants import TRANSLATION_LANGUAGES

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


def plan(user: dict, lang: str, *, can_subscribe: bool) -> str:
    """Subscription state, in the surface that owns payment.

    Plan §6.2 keeps buying in chat rather than in the Mini App, since Mini Apps
    selling digital goods sit closer to the Stars-only rule and to Apple's review
    guidelines. So this is the screen a paywall in the app points back to.
    """
    lines = [t(lang, "plan_title"), ""]

    expires = user.get("pass_expires_at")
    if user.get("has_pass") and expires:
        # ISO-8601 from the API; show the date only — the hour is noise to a reader
        # and a timezone argument waiting to happen.
        lines.append(t(lang, "plan_active", date=str(expires)[:10]))
    else:
        lines.append(t(lang, "plan_none"))
        lines.append(t(lang, "plan_free_left", n=user.get("free_explanations_left", 0)))

    lines += ["", t(lang, "plan_perks")]
    if not can_subscribe:
        lines += ["", t(lang, "payments_not_live")]
    return _clip("\n".join(lines), MESSAGE_LIMIT)
