"""The topic → article map.

The invariant that matters is coverage: every ministerial topic must resolve to
exactly one entry. A topic that matches none would be generated with no statute in
front of the model; a topic that matches two would silently take whichever came
first in a dict.
"""

from __future__ import annotations

import json

import pytest

from articles import TOPIC_ARTICLES, articles_for, expand, fold, signs_in

from shared.config import QUESTIONS_JSON


def topic_names() -> list[str]:
    data = json.loads(QUESTIONS_JSON.read_text(encoding="utf-8"))
    return [t["name"] for t in data["topics"]]


def test_every_ministerial_topic_maps_to_exactly_one_entry():
    unmatched, ambiguous = [], []
    for name in topic_names():
        matches = [key for key in TOPIC_ARTICLES if name.startswith(key)]
        if not matches:
            unmatched.append(name.split(";")[0])
        elif len(matches) > 1:
            ambiguous.append((name.split(";")[0], matches))
    assert not unmatched, f"topics with no articles mapped: {unmatched}"
    assert not ambiguous, f"topics matching several entries: {ambiguous}"


def test_the_table_has_no_entry_that_matches_nothing():
    """A leftover key is a mapping written for a topic name that no longer exists."""
    names = topic_names()
    orphans = [key for key in TOPIC_ARTICLES
               if not any(name.startswith(key) for name in names)]
    assert not orphans, f"TOPIC_ARTICLES keys matching no topic: {orphans}"


def test_every_topic_resolves_to_at_least_one_article():
    for name in topic_names():
        assert articles_for(name), name.split(";")[0]


def test_an_unknown_topic_fails_loudly():
    with pytest.raises(KeyError, match="TOPIC_ARTICLES"):
        articles_for("Un argomento che non esiste")


def test_ranges_expand_inclusively():
    assert expand("84-87") == ["84", "85", "86", "87"]
    assert expand("83") == ["83"]


def test_references_are_ordered_and_not_duplicated():
    references = articles_for("Segnali di pericolo")
    assert len(references) == len(set(references))
    assert ("reg", "84") in references and ("reg", "103") in references


# --- sign matching ---------------------------------------------------------

def test_folding_ignores_case_accents_and_punctuation():
    assert fold("LIMITE MASSIMO DI VELOCITÀ") == fold("limite massimo di velocita,")


def entry(name, plate, *articles):
    return {"name": name, "plate": plate, "articles": list(articles)}


def test_the_more_specific_sign_name_is_matched_first():
    """DIVIETO DI SORPASSO must beat DIVIETO DI TRANSITO and bare DIVIETO, or a
    cluster gets grounded on the article defining a different sign."""
    index = {
        fold("DIVIETO"): entry("DIVIETO", "II.44", ("reg", "115")),
        fold("DIVIETO DI SORPASSO"): entry("DIVIETO DI SORPASSO", "II.48", ("reg", "116")),
    }
    found = signs_in(["Il segnale raffigurato indica divieto di sorpasso"], index)
    assert found[0] == ("reg", "116")


def test_a_statement_naming_no_sign_finds_nothing():
    index = {fold("DIVIETO DI TRANSITO"): entry("DIVIETO DI TRANSITO", "II.46", ("reg", "116"))}
    assert signs_in(["Il segnale raffigurato è di pericolo"], index) == []


def test_one_naming_statement_brings_in_its_article():
    """Enough to *order* the article context, and no more. A mention is not an
    identification — cluster 624 is a stop sign whose statements mention the DIRITTO
    DI PRECEDENZA of the road it faces — so these matches must never be used to tell
    the generator which sign a figure shows. See generate.py's module docstring."""
    index = {fold("SENSO VIETATO"): entry("SENSO VIETATO", "II.47", ("reg", "116"))}
    cluster = [
        "Il segnale raffigurato vieta l'accesso",
        "Il segnale raffigurato è il segnale di senso vietato",
        "Il segnale raffigurato si trova solo in autostrada",
    ]
    assert signs_in(cluster, index) == [("reg", "116")]
