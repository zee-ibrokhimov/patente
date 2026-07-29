"""Message text.

Two rules the plan is emphatic about and that shape everything here:

  · The ministerial Italian is the question. A translation sits underneath it as a
    comprehension aid, in italics, visibly secondary — never in its place. Users
    are training to recognise the exact exam phrasing.

  · A locked explanation and a missing one look different. `locked` shows the
    paywall; `unavailable` shows nothing at all, because offering to sell an
    explanation nobody has written yet is worse than staying quiet.
"""

from __future__ import annotations

import html

from bot.i18n import t

# Telegram's photo-caption limit. Statements run ~120 characters and explanations
# two sentences, so this only ever bites on a pathological row — but silently
# losing the end of an explanation would be worse than an ellipsis.
CAPTION_LIMIT = 1024
MESSAGE_LIMIT = 4096


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def question(q: dict, lang: str, *, limit: int = CAPTION_LIMIT) -> str:
    parts = []
    if q.get("stem_it"):
        parts.append(f"<b>{html.escape(q['stem_it'])}</b>")
    parts.append(html.escape(q["statement_it"]))

    translation = q.get("translation")
    if q.get("translation_state") == "shown" and translation:
        parts.append(f"<i>{html.escape(translation['statement'])}</i>")

    return _clip("\n\n".join(parts), limit)


def result(q: dict, outcome: dict, lang: str, *, limit: int = CAPTION_LIMIT) -> str:
    """The answered question, rebuilt in place — statement stays visible."""
    parts = [html.escape(q["statement_it"])]

    if outcome["correct"]:
        parts.append(t(lang, "verdict_correct"))
    else:
        answer_word = t(lang, "answer_vero" if outcome["correct_answer"] else "answer_falso")
        parts.append(t(lang, "verdict_wrong", answer=answer_word))

    state = outcome["explanation_state"]
    if state == "shown" and outcome.get("explanation"):
        parts.append(html.escape(outcome["explanation"]))
    elif state == "locked":
        # The conversion moment: they just got it wrong and want to know why.
        parts.append(t(lang, "paywall"))

    return _clip("\n\n".join(parts), limit)


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
    from bot.i18n import LANGUAGE_NAMES

    state = t(lang, "state_on" if user["translations_on"] else "state_off")
    return "\n\n".join([
        t(lang, "settings_title"),
        t(lang, "settings_language", lang=LANGUAGE_NAMES.get(user["lang"], user["lang"])),
        t(lang, "settings_translations", state=state),
    ])
