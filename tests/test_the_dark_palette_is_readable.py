"""Every colour in the dark theme can actually be read on the surface it sits on.

Dark mode is a token flip — every rule in style.css already reads these — which makes the
VALUES the whole design. And a value that fails contrast fails silently: nothing errors,
the build is clean, and the app merely becomes unreadable for the people using it at night.

So the ratios are computed here rather than eyeballed. WCAG 2.1: 4.5:1 for body text,
3:1 for large text and non-text UI. The numbers are measured against both surfaces a colour
can land on — the page and a card — because passing on one and failing on the other is the
usual way this goes wrong.

`--text` at #0f172a on a #0f1420 page measures 1.05:1. That is not "low contrast", it is
invisible, and it is what shipping the light palette into a dark theme would have done to
the body copy of every screen.
"""

from __future__ import annotations

import pathlib
import re

import pytest

TOKENS = (pathlib.Path(__file__).resolve().parent.parent
          / "webapp" / "src" / "tokens.css").read_text(encoding="utf-8")
DARK = TOKENS[TOKENS.index('[data-theme="dark"]'):]
LIGHT = TOKENS[:TOKENS.index('[data-theme="dark"]')]


def values(block: str) -> dict[str, str]:
    return dict(re.findall(r"(--[\w-]+):\s*(#[0-9a-fA-F]{6})", block))


def luminance(hex_colour: str) -> float:
    parts = [int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    parts = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in parts]
    return 0.2126 * parts[0] + 0.7152 * parts[1] + 0.0722 * parts[2]


def contrast(a: str, b: str) -> float:
    la, lb = luminance(a), luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


# Colours used as TEXT, and the minimum each must clear.
#   4.5 — body copy and captions
#   3.0 — furniture that is seen rather than read: chevrons, gauge tracks, icon strokes
# The threshold is set by how each colour is USED, not by picking the strictest number and
# calling it rigour. A bar nobody's design meets gets weakened the first time it fails, and
# then it protects nothing.
#
#   4.5 — appears as body copy or a caption somewhere
#   3.0 — only ever large text, a fill with white on it, or furniture that is seen not read
#
# --gold is 2.38:1 on white and is deliberately absent: it is the Premium colour, it is used
# as text in five places, and --gold-dark (4.07:1) exists for exactly that. Repainting the
# brand is the owner's call, not a test's, so it is written up rather than asserted.
TEXT_TOKENS = {
    "--text": 4.5,
    "--text-2": 4.5,
    "--text-3": 3.0,      # chevrons and the settings globe
    "--accent": 4.5,      # link and button text
    "--bad": 4.5,         # error copy
    "--warn": 3.0,        # only .timer-value.warn, at display size
    "--ok": 3.0,          # mostly a fill; as text it is large
    "--exam-deep": 4.5,
    "--practice-deep": 4.5,
    "--gold-dark": 3.0,   # the Premium ink, on gold-tinted surfaces
}


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("token,minimum", sorted(TEXT_TOKENS.items()))
def test_the_colour_is_readable_on_both_surfaces(theme: str, token: str, minimum: float):
    """Both surfaces, because passing on the page and failing on a card is the usual way
    this goes wrong — and cards are where most of the text actually is."""
    block = DARK if theme == "dark" else LIGHT
    palette = values(block)
    if token not in palette:
        pytest.fail(f"{token} has no value in the {theme} theme — it keeps the other "
                    f"theme's colour, which is exactly the silent failure this guards")

    for surface in ("--bg", "--card"):
        got = contrast(palette[token], palette[surface])
        assert got >= minimum, (
            f"{theme}: {token} ({palette[token]}) on {surface} ({palette[surface]}) "
            f"is {got:.1f}:1, below {minimum}:1")


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_the_surfaces_are_distinguishable(theme: str):
    """A card that cannot be told from the page is not a card. Small on purpose — this is
    depth, not contrast — but it has to be non-zero, and in dark it replaces the shadow,
    which is switched off there because a light-tuned shadow reads as a smudge."""
    palette = values(DARK if theme == "dark" else LIGHT)
    got = contrast(palette["--bg"], palette["--card"])
    assert got >= 1.03, f"{theme}: card and page are the same colour ({got:.2f}:1)"


def test_the_two_themes_define_the_same_names():
    """A token defined only in light silently keeps its light value under dark — a white
    card on a near-black page, with nothing failing anywhere."""
    light, dark = values(LIGHT), values(DARK)
    core = {"--bg", "--card", "--surface", "--edge", "--text", "--text-2", "--text-3",
            "--accent", "--ok", "--bad", "--warn", "--gold"}
    missing = sorted(core - set(dark))
    assert not missing, f"no dark value for: {', '.join(missing)}"
    assert core <= set(light), "the light palette is incomplete"
