"""The statement on screen must be the statement in the database, character for character.

SHIPPED BROKEN AND REPORTED FROM A PHONE. Making every word holdable meant splitting the
statement into spans and reassembling it, and the reassembly dropped a case:

    "Il limite massimo di velocità … sulle autostrade è di 110110 km/h"

`lead` (the leading run of non-letters) and `tail` (the trailing run) were both measured
against the whole token. For a token with no letters at all — "110", "3,5", "50." — both
matched the ENTIRE token, `core` came out empty, and the token was emitted twice.

594 of the 7,106 statements contain such a token: every speed limit, engine size, mass and
duration in the product. A learner reading "110110 km/h" cannot answer the question, and it
looks like the content is wrong rather than the app.

WHY NO TEST CAUGHT IT: every fixture statement was prose. Not one contained a bare number,
which is the only shape that triggers it. So this file renders the REAL bank — all 7,106 —
because the property is about the whole corpus and a curated sample is exactly what missed
it the first time.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MAIN = (ROOT / "webapp/src/main.ts").read_text(encoding="utf-8")

# `\p{L}` in JS is `[^\W\d_]` in Python's re — any letter, no digits, no underscore.
LETTERS = r"^\W\d_"
LEAD = re.compile(f"^[{LETTERS}]*")
TAIL = re.compile(f"[{LETTERS}]*$")


def render(text: str) -> str:
    """A faithful mirror of `tappableStatement`, reassembled.

    Python, because there is no JS test runner in this project and no node on the host. The
    mirror is only as good as its fidelity, so the test below ALSO reads the TypeScript and
    asserts the one line that matters — a mirror that silently drifted from the source would
    otherwise pass for ever while the app stayed broken.
    """
    out = []
    for chunk in re.split(r"(\s+)", text):
        if not chunk:
            continue
        if chunk.isspace():
            out.append(chunk)
            continue
        lead = LEAD.match(chunk).group(0)
        rest = chunk[len(lead):]
        tail = TAIL.search(rest).group(0)
        core = rest[: len(rest) - len(tail)]
        out.append(lead)
        out.append(core)      # a span in the app; the same characters either way
        out.append(tail)
    return "".join(out)


# --- the shapes that broke ------------------------------------------------------

@pytest.mark.parametrize("text", [
    "è di 110 km/h",
    "non deve superare 3,5 t",
    "per più di 5 ore (art. 158 C.d.S.)",
    "cilindrata di almeno 150 cm3",
    "50.",
    "30",
    "2 h.",
    "«Virgolette» e — trattini, 50.",
    "Il segnale raffigurato vieta il transito",
    "Sosta 30 min; max 2 h.",
    "L'art. 142 fissa il limite a 130 km/h.",
])
def test_a_statement_survives_being_made_holdable(text):
    assert render(text) == text


def test_a_bare_number_is_not_doubled():
    """THE defect, named. "110" became "110110" on 594 statements."""
    assert render("110") == "110"
    assert render("è di 110 km/h") == "è di 110 km/h"


# --- the whole bank -------------------------------------------------------------

def _statements() -> list[str]:
    db = ROOT / "patente.db"
    if not db.exists():
        pytest.skip("no local content bank")
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as c:
        rows = c.execute(
            "SELECT statement_it FROM questions "
            "UNION ALL SELECT stem_it FROM questions WHERE stem_it IS NOT NULL"
        ).fetchall()
    return [r[0] for r in rows if r[0]]


def test_every_statement_in_the_bank_renders_unchanged():
    """All 7,106, not a sample. A curated sample of prose is what missed this."""
    statements = _statements()
    assert len(statements) > 5000, f"only {len(statements)} statements — is the bank seeded?"

    # Counted, not assumed. Silently narrowing this to the first fifty would still pass
    # against correct code, so the number actually examined is asserted alongside the
    # result — the claim being made here is "all of them", and that claim is checkable.
    checked = 0
    broken = []
    for text in statements:
        checked += 1
        if render(text) != text:
            broken.append(text)
    assert checked == len(statements)
    assert not broken, (
        f"{len(broken)} of {checked} statements are corrupted on screen. "
        f"First: {broken[0]!r} renders as {render(broken[0])!r}"
    )


def test_every_translation_renders_unchanged():
    """Translations go through the same splitter when they are shown, and Russian and Uzbek
    have their own punctuation — «», ʻ — that a letter-class regex could mishandle."""
    db = ROOT / "patente.db"
    if not db.exists():
        pytest.skip("no local content bank")
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as c:
        rows = [r[0] for r in c.execute("SELECT statement FROM translations") if r[0]]
    if not rows:
        pytest.skip("no translations seeded locally")
    broken = [s for s in rows if render(s) != s]
    assert not broken, f"{len(broken)} translations corrupted, first: {broken[0]!r}"


# --- the mirror must not drift from the source ----------------------------------

def test_the_tail_is_measured_from_what_is_left_not_from_the_whole_token():
    """The one line that caused it, pinned.

    Measuring both runs against the original token lets them OVERLAP whenever the token has
    no letters, and the overlap is what emits it twice. Measuring the tail from what remains
    after the lead makes that impossible by construction.
    """
    body = MAIN[MAIN.index("function tappableStatement("):]
    body = body[:body.index("\nfunction ", 10)]
    assert "const rest = chunk.slice(lead.length);" in body
    assert "rest.match(/[^\\p{L}]*$/u)" in body, "the tail is not measured from `rest`"
    assert "chunk.match(/[^\\p{L}]*$/u)" not in body, \
        "the tail is measured against the whole token again — this is the 110110 bug"


def test_the_mirror_still_matches_the_source_shape():
    """A mirror that drifted would pass for ever while the app stayed broken, so the pieces
    it depends on are asserted to exist."""
    body = MAIN[MAIN.index("function tappableStatement("):]
    body = body[:body.index("\nfunction ", 10)]
    for piece in ("text.split(/(\\s+)/)", "chunk.match(/^[^\\p{L}]*/u)",
                  "rest.slice(0, rest.length - tail.length)"):
        assert piece in body, f"the mirror assumes {piece!r}, which is no longer in the source"
