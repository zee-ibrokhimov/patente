"""The home screen's mode cards have a column for every child they render.

A CSS grid does not complain about a child it has no column for — it silently wraps it onto
a new row. `.mode` declared two columns while `modeCard` appends three children, so the
chevron dropped to row 2, column 1 and rendered UNDER THE ARTWORK at the bottom-left of the
card, pointing away from it. Reported from a screenshot with the arrow circled and a line
drawn to where it belonged.

It survived because nothing about it looks wrong in either file. The card renders three
things, in order, correctly. The stylesheet declares a sensible two-column grid. The bug
only exists in the relationship between them, and `.mode-go` still carried `margin-left:
auto` and `flex: none` — flexbox leftovers that are inert in a grid cell and read, at a
glance, like the positioning was handled.

There is no browser in this suite, so this asserts the one thing that can be checked without
one: the counts agree. It is a narrow test and that is the point — it catches the exact
class of mistake that produced the defect, which is adding a child and not the column.
"""

from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
CSS = (ROOT / "webapp/src/style.css").read_text(encoding="utf-8")
MAIN = (ROOT / "webapp/src/main.ts").read_text(encoding="utf-8")


def rule(selector: str) -> str:
    """The body of a top-level CSS rule, comments stripped."""
    m = re.search(rf"^{re.escape(selector)} \{{(.*?)^\}}", CSS, re.M | re.S)
    assert m, f"no rule for {selector}"
    return re.sub(r"/\*.*?\*/", "", m.group(1), flags=re.S)


def mode_card_children() -> list[str]:
    body = MAIN[MAIN.index("function modeCard"):]
    body = body[:body.index("\n}")]
    return re.findall(r"card\.append\((\w+)\)", body)


def grid_columns(body: str) -> list[str]:
    m = re.search(r"grid-template-columns:\s*([^;]+);", body)
    assert m, "no grid-template-columns"
    # clamp(...) and minmax(...) are single tracks containing commas — collapse them first.
    flat = re.sub(r"\w+\([^()]*(?:\([^()]*\)[^()]*)*\)", "T", m.group(1))
    return flat.split()


def test_the_card_has_a_column_for_every_child():
    """THE bug. Two columns, three children — the third wrapped to a second row and landed
    under the artwork."""
    children = mode_card_children()
    columns = grid_columns(rule(".mode"))
    assert len(columns) == len(children), (
        f"{len(children)} children ({', '.join(children)}) into {len(columns)} columns "
        f"{columns} — the extra child wraps onto a new row")


def test_the_arrow_is_the_last_child():
    """Grid auto-placement follows DOM order, so the chevron being last is what puts it in
    the trailing column rather than between the artwork and the text."""
    assert mode_card_children()[-1] == "go"


def test_the_arrow_column_hugs_its_content():
    """A 30px circle in a `1fr` track would sit in the middle of the leftover space rather
    than at the edge."""
    assert grid_columns(rule(".mode"))[-1] == "auto"


def test_the_text_column_is_the_one_that_flexes():
    """Everything that can shrink should shrink there — the artwork is clamped and the
    arrow is fixed."""
    assert grid_columns(rule(".mode"))[1] == "1fr"


def test_the_arrow_carries_no_dead_flexbox_positioning():
    """`margin-left: auto` and `flex: none` are inert inside a grid cell. They were left
    over from when this card was a flex row, and they made the arrow LOOK positioned — part
    of why the misplacement read as intentional."""
    body = rule(".mode-go")
    assert "margin-left" not in body
    assert "flex:" not in body


def test_the_card_still_centres_its_row():
    """What actually centres the arrow against the text now that nothing else does."""
    assert "align-items: center" in rule(".mode")


def test_the_text_column_can_still_shrink():
    """min-width:0 — without it a long word holds the column at its intrinsic width and the
    card overflows the screen, which is a different bug on the same row."""
    assert "min-width: 0" in rule(".mode-body")
