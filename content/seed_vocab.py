"""Load the vocabulary list into the database.

THE LIST IS NOT OURS. It was compiled by Zukhriddin Kamolov (Telegram @TTYMI_OKMK2) and is
used with his permission, on the condition that he is credited as its author — a term of
use, not a courtesy. The credit renders on the vocabulary screen in the Mini App and
tests/test_vocab_attribution.py fails if it disappears. Read content/VOCAB-CREDITS.md before
changing that, or the Italian column, which is his work and stays verbatim.

Idempotent, like content/seed.py: run it as often as you like. Terms are matched on the
Italian, so re-running updates glosses in place and never orphans a learner's progress —
which is the whole reason the match is on `it` and not on `rank`. Ranks shift whenever the
sheet is re-exported; the Italian word does not.

Nothing here calls a model. The glosses were generated once, reviewed, and committed to
content/vocab.json. Regenerating them on every deploy would spend money to produce
slightly different wording each time, and a vocabulary list that quietly rewords itself
under a learner is worse than one that is occasionally imperfect.
"""

from __future__ import annotations

import json
import pathlib
import sys

from sqlalchemy import select

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from api.models import VocabTerm  # noqa: E402
from shared.db import sync_session_factory  # noqa: E402

DATA = pathlib.Path(__file__).resolve().parent / "vocab.json"


def load() -> list[dict]:
    terms = json.loads(DATA.read_text(encoding="utf-8"))
    seen: set[str] = set()
    clean: list[dict] = []
    for t in terms:
        key = t["it"].strip().lower()
        if key in seen:
            continue
        # A term with a missing gloss cannot be tested in that direction and would grade
        # every answer wrong. Drop it loudly rather than seed a broken row.
        if not all(t.get(k, "").strip() for k in ("it", "en", "ru", "uz")):
            print(f"  skipping incomplete entry: {t.get('it')!r}")
            continue
        seen.add(key)
        clean.append(t)
    return clean


def seed() -> tuple[int, int]:
    terms = load()
    added = updated = 0
    factory = sync_session_factory()
    with factory() as session:
        existing = {
            t.it.strip().lower(): t
            for t in session.scalars(select(VocabTerm)).all()
        }
        for t in terms:
            row = existing.get(t["it"].strip().lower())
            if row is None:
                session.add(VocabTerm(rank=t["rank"], it=t["it"].strip(),
                                      en=t["en"].strip(), ru=t["ru"].strip(),
                                      uz=t["uz"].strip()))
                added += 1
            else:
                # `it` is updated too, not just the glosses. The match is
                # case-insensitive so that a re-exported sheet does not orphan progress,
                # but the file is the canonical spelling — without this, a term the sheet
                # once wrote as "Sosta" keeps that capital forever while the entry that
                # replaced it is lowercase, and the word list shows one odd row.
                before = (row.it, row.rank, row.en, row.ru, row.uz)
                row.it, row.rank, row.en, row.ru, row.uz = (
                    t["it"].strip(), t["rank"], t["en"].strip(), t["ru"].strip(),
                    t["uz"].strip())
                if before != (row.it, row.rank, row.en, row.ru, row.uz):
                    updated += 1
        session.commit()
    return added, updated


if __name__ == "__main__":
    a, u = seed()
    print(f"vocabulary: {a} added, {u} updated")
