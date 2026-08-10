"""No class name is defined twice for two different purposes.

A duplicate CSS rule does not warn — it MERGES. Later declarations win per property, and
everything else is inherited from whichever block happened to come first.

That shipped. A new `.tile` was added for the coloured icon on the home cards, and `.tile`
was already the stats screen's headline box further down the same file. The result was two
broken screens from one name:

  · the home tiles took the stats block's `background: var(--card)` and rendered as empty
    bordered squares — the icon was there, invisible, white on white;
  · the stats tiles took the new `width: 52px; height: 52px` and were squashed into little
    boxes with their numbers pushed outside.

Nothing failed. tsc was clean, vite built, 1392 tests passed, and it was found by looking
at a photograph of a phone.

This does not ban duplicate selectors — a base rule plus a media query or a state variant
is normal and correct. It bans the same class being DEFINED in two separate places, which
is what a collision looks like.
"""

from __future__ import annotations

import pathlib
import re
from collections import defaultdict

CSS = (pathlib.Path(__file__).resolve().parent.parent
       / "webapp" / "src" / "style.css").read_text(encoding="utf-8")


def top_level_class_rules() -> dict[str, list[int]]:
    """Which line each `.foo { ... }` rule starts on, for bare single-class selectors only.

    Deliberately narrow. `.a .b`, `.a.b`, `.a:hover`, `.a > .b` and anything inside a media
    query are all legitimate ways to write about a class more than once; a bare `.foo` block
    at the top level is a DEFINITION, and two of those are a collision.
    """
    seen: dict[str, list[int]] = defaultdict(list)
    depth = 0
    for number, line in enumerate(CSS.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("/*") or not stripped:
            depth += line.count("{") - line.count("}")
            continue
        if depth == 0:
            match = re.match(r"^(\.[A-Za-z][\w-]*)\s*\{", stripped)
            if match:
                seen[match.group(1)].append(number)
        depth += line.count("{") - line.count("}")
    return seen


def test_no_class_is_defined_twice():
    duplicates = {
        name: lines for name, lines in top_level_class_rules().items() if len(lines) > 1
    }
    assert not duplicates, "\n".join(
        f"{name} is defined at lines {lines} — a duplicate rule merges silently, and "
        f"whichever block comes second wins on the properties they share"
        for name, lines in sorted(duplicates.items())
    )


def test_the_guard_can_see_a_collision():
    """Guards the guard. If the parser stops recognising rules — a formatting change, a
    nested syntax — this file passes for ever while seeing nothing."""
    found = top_level_class_rules()
    assert len(found) > 100, (
        f"only {len(found)} class rules parsed out of a 1200-line stylesheet — "
        f"the parser has stopped matching")
    assert ".card" in found, "a known class was not found; the parser is broken"
