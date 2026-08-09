"""A control placed next to text sizes to its label, not to the whole row.

The Admin card on Settings rendered as a sliver of wrapped text — "Grant / ac / tri /
links, / newsletter" — with the Open button laid across it. Reported from a screenshot.

The mechanism is a collision between two rules that are each correct alone:

    .btn      { width: 100%; }        <- it is the big control at the bottom of a screen
    .row      { display: flex; }
    .row-main { flex: 1; min-width: 0; }   <- allowed to shrink to nothing

In a flex row, `width: 100%` resolves against the ROW, so the button demands all of it and
`.row-main` — explicitly permitted to shrink — gives way. Nothing overflows and nothing
errors; the text just gets a column three characters wide.

Every other `.row` in the app escaped because it pairs with `.switch`, which sets
`flex: none`. That is why this was not caught earlier: the one row that used a button was
the one row nobody but the owner ever sees.
"""

from __future__ import annotations

import pathlib
import re

WEB = pathlib.Path(__file__).resolve().parent.parent / "webapp" / "src"
CSS = (WEB / "style.css").read_text(encoding="utf-8")
MAIN = (WEB / "main.ts").read_text(encoding="utf-8")


def rule_for(selector: str) -> str:
    """The declaration block of one selector, or ''. """
    match = re.search(rf"^{re.escape(selector)}\s*\{{(.*?)\}}", CSS, re.S | re.M)
    return match.group(1) if match else ""


def test_the_premise_still_holds():
    """Guards the guard. If .btn stops being full-width, or .row-main stops being
    shrinkable, this whole file is asserting against a bug that cannot happen — and would
    pass for the wrong reason."""
    assert "width: 100%" in rule_for(".btn"), ".btn is no longer full-width"
    row_main = rule_for(".row-main")
    assert "flex: 1" in row_main and "min-width: 0" in row_main, \
        ".row-main no longer shrinks; re-derive what this file protects"
    assert "display: flex" in rule_for(".row"), ".row is no longer a flex row"


def test_a_button_in_a_row_is_constrained():
    """THE fix. Without this the button takes the row and the label collapses."""
    rule = rule_for(".row > .btn")
    assert rule, "nothing constrains a .btn inside a .row"
    assert "width: auto" in rule, f"the button is still full-width in a row: {rule.strip()}"
    assert "flex: none" in rule, f"the button can still grow to fill the row: {rule.strip()}"


def test_the_constrained_button_is_still_tappable():
    """Shrinking it to its label must not shrink it below a thumb. 44px is the floor every
    platform's guidance agrees on, and this control is how staff reach the panel."""
    rule = rule_for(".row > .btn")
    height = re.search(r"height:\s*(\d+)px", rule)
    assert height and int(height.group(1)) >= 44, f"tap target too small: {rule.strip()}"


def test_the_admin_card_is_the_shape_this_protects():
    """Anchored to the real call site, so the test dies with the thing it guards rather
    than passing for ever over deleted markup."""
    start = MAIN.index('el("div", "row-title", "Admin")')
    block = MAIN[start - 400:start + 600]
    assert 'el("div", "row")' in block, "the Admin card no longer uses a .row"
    assert '"btn secondary"' in block, "the Admin card no longer puts a button in that row"
