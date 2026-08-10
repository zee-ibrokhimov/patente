"""The facts shown while a quiz is being prepared.

These are the one place in the app where text is presented as true with no question attached
to it and no explanation underneath. A learner reads a tip in the two seconds it is on
screen, believes it, and repeats it — so a wrong one does not merely fail to help, it
teaches something that will cost them the exam.

Fourteen candidates were drafted and put through four independent adversarial checks
(traffic-law numbers, neopatentato-specific divergence, sign taxonomy, exam format). Twelve
came back challenged as MISLEADING rather than wrong — stated truly but framed so a learner
would generalise incorrectly — and were repaired. One was dropped outright: it claimed the
neopatentato power limits are 75 kW/t and 105 kW for M1, no checker flagged it, no question
in patente.db states those figures, and they conflict with the values in art. 117. An
unverifiable number is worth less than a missing tip.

What follows guards the properties a future edit could break without anyone noticing:
parity across languages, brevity, and the neopatentato caveats that the checks put there.
"""

from __future__ import annotations

import pathlib
import re

import pytest

SRC = pathlib.Path(__file__).resolve().parent.parent / "webapp" / "src"
LANGS = ("it", "ru", "en", "uz")


def tips() -> dict[str, list[str]]:
    text = (SRC / "i18n.ts").read_text(encoding="utf-8")
    block = text[text.index("export const TIPS"):]
    block = block[:block.index("\n};")]
    out: dict[str, list[str]] = {}
    for lang in LANGS:
        chunk = block[block.index(f"  {lang}: ["):]
        chunk = chunk[:chunk.index("\n  ],")]
        out[lang] = re.findall(r'^\s*"(.+)",$', chunk, re.M)
    return out


def test_every_language_has_the_same_facts():
    """A missing entry does not degrade gracefully: `tips()` falls back to English only when
    a list is EMPTY, so a short list silently teaches a Russian learner fewer facts than an
    English one rather than falling back."""
    counts = {lang: len(rows) for lang, rows in tips().items()}
    assert len(set(counts.values())) == 1, counts
    assert counts["ru"] >= 10, "too few to be worth the rotation"


@pytest.mark.parametrize("lang", LANGS)
def test_a_tip_can_be_read_in_the_time_it_is_shown(lang):
    """They rotate every 4.5 seconds into a slot that reserves 3.4em. Something longer is
    either clipped or resizes the box under the reader."""
    for tip in tips()[lang]:
        assert len(tip) <= 95, f"{lang}: {len(tip)} chars — {tip}"


@pytest.mark.parametrize("lang", LANGS)
def test_no_tip_is_empty_or_a_placeholder(lang):
    for tip in tips()[lang]:
        assert tip.strip()
        assert "TODO" not in tip and "{" not in tip


def test_the_speed_and_alcohol_tips_name_the_neopatentato_case():
    """The repair that four separate checks asked for, and the one most likely to be
    undone by someone tidying the strings for length.

    Every user of this app is a learner, so every one of them will spend three years as a
    neopatentato. A tip that says the motorway limit is 130 and stops there states the one
    figure that does NOT apply to its entire audience.
    """
    rows = tips()["ru"]
    speed = [t for t in rows if "130" in t]
    assert speed, "the default speed-limit tip is gone"
    assert any("еопатентато" in t for t in speed), speed

    alcohol = [t for t in rows if "0,5" in t]
    assert alcohol, "the general alcohol-limit tip is gone"
    assert any("0,0" in t for t in alcohol), alcohol


def test_the_triangle_sign_tip_distinguishes_which_way_it_points():
    """All four checks flagged the same thing: "triangle = danger" is false for the
    point-down triangle, which is `dare precedenza` — a segnale di precedenza, not a danger
    sign, and one of the most commonly failed items in the real exam. Teaching the shortcut
    would actively cause the mistake."""
    row = [t for t in tips()["ru"] if "реугольник" in t]
    assert row, "the sign-shape tip is gone"
    assert any("вниз" in t for t in row), row


def test_the_dropped_tip_stays_dropped():
    """The neopatentato power limits. If someone restores this, it has to come back with a
    number checked against art. 117 rather than against a model's recollection."""
    for lang, rows in tips().items():
        for tip in rows:
            assert "kW" not in tip and "кВт" not in tip, f"{lang}: {tip}"


def test_the_rotation_is_wired_to_the_list():
    """A list nothing reads is decoration. Guards the three pieces that connect it: the
    accessor, the interval, and the teardown that stops it poking a detached node."""
    main = (SRC / "main.ts").read_text(encoding="utf-8")
    assert "startTips(" in main and "stopTips()" in main
    assert "TIP_EVERY" in main
    # render() must stop the rotator when the preparing screen goes away.
    block = main[main.index("function render()"):][:1200]
    assert "stopTips()" in block
