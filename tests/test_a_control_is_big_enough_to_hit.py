"""Every control a thumb has to hit is at least 44px.

FOUND BY AN ADVERSARIAL PASS OVER A DENSITY CHANGE, not by the density change itself —
which is the point. Making screens shorter and making controls smaller are the same edit
seen from two angles, and the second one is invisible in a diff.

Three controls were ALREADY below the floor before any of that work, and had been since
they were drawn:

  * `.switch`   52x31 — 13px under in the short axis, on every toggle in settings
  * `.who-gear` a 24px icon at 6px padding = 36x36, the only way into settings from profile
  * `.subject-topic` 8px padding around 14px/1.35 text = ~37px, a tappable row

44px is Apple's floor and Google's is 48dp; 44 is the number this project uses. The fix for
each was free, because in every case the row around the control was already taller.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_RAW = (Path(__file__).resolve().parent.parent / "webapp/src/style.css").read_text(encoding="utf-8")
# Comments are stripped before parsing. Every rule this file cares about is preceded by a
# comment explaining why it exists, and those comments quote CSS — so a parser that reads
# them as part of the next selector finds nothing and the whole file passes vacuously.
CSS = re.sub(r"/\*.*?\*/", "", _RAW, flags=re.S)


def rule(selector: str) -> str:
    """The body of the first rule whose selector list contains `selector` exactly."""
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", CSS):
        heads = [h.strip() for h in m.group(1).replace("\n", " ").split(",")]
        if selector in heads:
            return m.group(2)
    raise AssertionError(f"no rule for {selector!r}")


def px(body: str, prop: str) -> int | None:
    m = re.search(rf"(?<!-){prop}:\s*(-?\d+(?:\.\d+)?)px", body)
    return round(float(m.group(1))) if m else None


# --- the three that were broken -------------------------------------------------

def test_the_toggle_switch_is_tappable():
    """The switch itself stays 52x31 — that is what an iOS switch looks like and shrinking
    the LOOK to fix the FEEL would be the wrong trade. A transparent pseudo-element takes
    the taps instead, and `inset` is load-bearing: at -8px the hit box is 47px tall, which
    is 3px of slack, so a later "tidy up" that drops it breaks the floor."""
    body = rule(".switch")
    assert px(body, "width") == 52 and px(body, "height") == 31, \
        "the switch's visual size changed; re-check the hit box below"

    before = rule(".switch::before")
    m = re.search(r"inset:\s*(-?\d+)px\s+(-?\d+)px", before)
    assert m, ".switch::before no longer grows the hit area"
    dy, dx = int(m.group(1)), int(m.group(2))
    assert 31 - 2 * dy >= 44, f"hit box is {31 - 2 * dy}px tall, under the 44px floor"
    assert 52 - 2 * dx >= 44, f"hit box is {52 - 2 * dx}px wide, under the 44px floor"
    assert "overflow" not in rule(".switch"), \
        "overflow on .switch would clip the pseudo-element that is doing the work"


@pytest.mark.parametrize("selector", [".who-gear", ".subject-topic", ".subject-more",
                                      ".subject-head"])
def test_the_control_clears_the_floor(selector):
    body = rule(selector)
    got = px(body, "min-height")
    assert got is not None, f"{selector} declares no min-height"
    assert got >= 44, f"{selector} is {got}px, under the 44px floor"


@pytest.mark.parametrize("selector", [".subject-topic", ".subject-more", ".subject-head",
                                      ".who-gear"])
def test_the_floor_is_a_minimum_not_a_height(selector):
    """A fixed height would clip Russian and Uzbek, which run 20-40% longer than Italian —
    and the ministerial topic names reach sixty characters in every language."""
    body = rule(selector)
    assert px(body, "height") is None, \
        f"{selector} sets a fixed height; long names in ru/uz will be clipped"


def test_the_tab_bar_still_clears_the_floor_after_being_tightened():
    """The tab bar cost 61px of a 730px screen and was tightened to buy that back on EVERY
    screen at once.

    Note what this can and cannot catch. A tab is padding + icon + gap + label, and the
    icon (23) + gap (4) + label (12 x 1.45 = 17) already sum to 44 on their own — so no
    padding value, not even zero, can breach the floor. Mutating the padding to `1px 4px
    0px` was tried and survived, correctly. The load-bearing parts are therefore the ICON
    SIZE and the label, and those live in main.ts, so both are read from there rather than
    hard-coded into an arithmetic that would agree with itself for ever.
    """
    main = (Path(__file__).resolve().parent.parent / "webapp/src/main.ts").read_text(encoding="utf-8")
    tabs = main[main.index("function tabs("):]
    tabs = tabs[:tabs.index("\nfunction ", 10)]

    icons = [int(n) for n in re.findall(r"icons\.\w+\((\d+)\)", tabs)]
    assert len(icons) == 5, f"expected five tabs, found {len(icons)} icons"
    assert len(set(icons)) == 1, f"the tab icons are different sizes: {icons}"
    icon = icons[0]

    assert 'el("span", "", label)' in tabs, "the tab label is gone; the height drops by ~21px"

    body = rule(".tab")
    m = re.search(r"padding:\s*(\d+)px\s+\d+px\s+(\d+)px", body)
    assert m, ".tab padding is no longer a three-value shorthand; re-derive the height"
    top, bottom = int(m.group(1)), int(m.group(2))
    size = re.search(r"font-size:\s*(\d+)px", body)
    assert size, ".tab sets no font-size"
    gap = px(body, "gap") or 0
    height = top + icon + gap + round(int(size.group(1)) * 1.45) + bottom
    assert height >= 44, (
        f"a tab is {height}px (padding {top}/{bottom}, icon {icon}, gap {gap}, "
        f"label {size.group(1)}px), under the 44px floor"
    )
