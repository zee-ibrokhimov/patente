"""Load the vocabulary list into the database.

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
                before = (row.rank, row.en, row.ru, row.uz)
                row.rank, row.en, row.ru, row.uz = (
                    t["rank"], t["en"].strip(), t["ru"].strip(), t["uz"].strip())
                if before != (row.rank, row.en, row.ru, row.uz):
                    updated += 1
        session.commit()
    return added, updated


if __name__ == "__main__":
    a, u = seed()
    print(f"vocabulary: {a} added, {u} updated")
