"""Making a Telegram first name safe to show to a stranger.

This is the only string in the product that one learner types and another learner reads.
Everything else on the board is a number this server computed.

WHAT TELEGRAM PROMISES ABOUT A FIRST NAME: nothing. It is user-controlled text of
unspecified length, and it accepts the whole of Unicode. The field arrived here with
`.strip()[:32]` in front of it and nothing else, which handles length and leaves the rest.

WHAT IS ACTUALLY REMOVED, AND WHY EACH ONE

  · Control and format characters (Unicode categories Cc and Cf). This is the class that
    contains U+202E RIGHT-TO-LEFT OVERRIDE and its relatives, which reverse the rendering of
    everything after them — so a name can reach out of its own cell and reorder the row it
    sits in. It also contains zero-width spaces, which make a name that looks blank, and the
    bidi isolates. There is no legitimate name in any language that needs one.
  · Newlines and tabs specifically, which are in Cc but are worth naming: the board renders
    one row per learner, and a name containing three newlines is a name occupying four rows.
  · Leading combining marks (Mn, Me). Applied to nothing, they stack onto whatever character
    the renderer finds to their left — which is somebody else's text.
  · Runs of whitespace, collapsed to one space.

WHAT IS DELIBERATELY KEPT

Emoji. Stripping them is tempting — someone can call themselves "🥇 Aziz" and appear to be
wearing a medal — but a filter aggressive enough to catch that also mangles ordinary names,
and the medal problem has a better answer: the medal is its own field and its own element on
the row, never concatenated into the name, so a fake one sits visibly in the wrong place.

Non-Latin scripts, obviously. This product's users write in Cyrillic and in Uzbek Latin with
oʻ and gʻ, and a "sanitiser" that dropped them would be the bug.

RETURNS None, NOT AN EMPTY STRING, when nothing usable is left. `display_name` is nullable
and the board already renders a neutral placeholder for a learner with no name; an empty
string instead produces a blank row, which is a different bug wearing the first one's clothes.

NOT HTML-ESCAPED HERE. The escape belongs at the point of interpolation into HTML — the bot
sends `parse_mode="HTML"` — and doing it here would double-escape the name in the JSON API,
where the client renders through `textContent` and needs the real characters.
"""

from __future__ import annotations

import unicodedata

# Code point categories that never belong in a name shown to somebody else.
#   Cc  control (newline, tab, the C0/C1 sets)
#   Cf  format  (U+200B zero width space, U+202A-U+202E bidi overrides, U+2066-U+2069)
#   Cs  surrogate — unpaired, cannot render
#   Co  private use — renders as whatever the reader's font decides
#   Cn  unassigned
STRIPPED_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn"})

# Marks, which combine leftwards onto a base character. Removed only at the START, where
# there is no base character of their own to attach to.
MARK_CATEGORIES = frozenset({"Mn", "Me"})

# Rendered inside other learners' apps, and Telegram does not bound its own field. Counted
# in code points rather than bytes so the limit means the same thing in every script.
MAX_LENGTH = 32


def clean(raw: str | None) -> str | None:
    """A first name fit to put in front of a stranger, or None if nothing survives."""
    if not raw:
        return None

    # NFC first: composed form is what the rest of the product stores, and normalising after
    # truncation could cut a base character away from its own accent.
    text = unicodedata.normalize("NFC", raw)

    kept: list[str] = []
    for char in text:
        category = unicodedata.category(char)
        if category in STRIPPED_CATEGORIES:
            # Whitespace-ish controls become a space so "Aziz\n\nBek" does not become
            # "AzizBek" — the words were separate and the name should stay readable.
            if char in "\t\n\r\v\f":
                kept.append(" ")
            continue
        kept.append(char)

    # Collapse runs of whitespace, including the ones just introduced above.
    text = " ".join("".join(kept).split())

    # Drop marks with nothing to sit on. Repeated, because removing one can expose another.
    while text and unicodedata.category(text[0]) in MARK_CATEGORIES:
        text = text[1:].lstrip()

    text = text[:MAX_LENGTH].strip()

    # A "name" of punctuation or symbols alone is not a name. One learner in the existing
    # data is stored as "." — which on a leaderboard is indistinguishable from a rendering
    # fault, and the anonymous placeholder is both more honest and more readable.
    if not any(unicodedata.category(c)[0] in ("L", "N") for c in text):
        return None
    return text or None
