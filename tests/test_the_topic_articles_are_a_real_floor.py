"""A cluster is always shown the statute its own topic maps to.

Cluster 638 shipped an explanation that flatly contradicted the ministerial answer key, and
the model was not at fault. It was asked to explain a dashed white ROAD MARKING while being
shown articles about road SIGNS, because the article selector let one long sign-name match
eat the entire prompt budget:

    reg.135  "Segnali utili per la guida"    16,208 chars   TAKEN
    reg.122  "Segnali di obbligo generico"    4,528 chars   TAKEN
    reg.148  "Iscrizioni e simboli"           3,208 chars   TAKEN
    reg.137  "Segnali orizzontali"            2,696 chars   skipped — budget
    reg.138  "Strisce longitudinali"          3,262 chars   skipped — budget
    reg.139  "Strisce di separazione dei sensi di marcia"   skipped — budget
    ... every remaining article of the topic       skipped — budget

Article 139 is the one that says a dashed line separates the directions of travel. It was
not in the prompt. The model wrote "Non è destinata a separare i sensi di marcia" — a
correct reading of the wrong law, and the opposite of the answer to q20711 in its own
cluster.

The docstring already called the topic's articles "the floor". A floor that can be truncated
to nothing is not a floor, so it is now reserved.
"""

from __future__ import annotations

import pytest

from api.services.articles import articles_for, signs_in
from api.services.explanations import (
    CONTEXT_CHARS,
    FLOOR_SHARE,
    corpus_and_index,
    select_articles,
)

MARKINGS = "Segnaletica orizzontale; segni sugli ostacoli"

# The REAL statements the sampler picks for cluster 638, and they have to be the real ones.
#
# A first version of this fixture used three hand-picked dashed-line statements. They match
# no sign name at all, so `signs_in` returned [] and the floor got the whole budget whatever
# the code did — the tests passed against the bug and against the fix identically. The
# mutation is what exposed that: it applied cleanly and nothing failed.
#
# The trigger is the fifth line below. "inversione di marcia" is a sign name, so it matches
# art. 135 "Segnali utili per la guida" — 16,208 characters, two thirds of the budget.
DASHED_LINE = [
    "La striscia bianca discontinua in figura può essere superata",
    "La striscia bianca discontinua in figura divide la carreggiata in due corsie",
    "Nelle strade a doppio senso con due corsie la striscia bianca discontinua in figura "
    "divide i sensi di marcia",
    "La striscia bianca discontinua in figura consente l'inversione di marcia, in "
    "condizioni di sicurezza",
    "La striscia bianca discontinua in figura divide la strada da una pista ciclabile",
]


@pytest.fixture(scope="module")
def corpus():
    return corpus_and_index()


def numbers(chosen) -> list[str]:
    return [f"{a['source']}.{a['number']}" for a in chosen]


# --- the case that shipped wrong --------------------------------------------

def test_the_trigger_is_still_in_the_fixture(corpus):
    """Guards the guard. If `signs_in` stops matching art. 135 on these statements, every
    test below passes for the wrong reason — which is exactly what happened to the first
    version of this file."""
    matched = signs_in(DASHED_LINE, corpus[1])
    assert ("reg", "135") in matched, (
        f"the fixture no longer reproduces the bug: {matched}")
    assert len(corpus[0]["reg"]["135"]["text"]) > CONTEXT_CHARS * 0.5, \
        "art. 135 is no longer large enough to eat the budget"


def test_a_markings_cluster_is_shown_the_markings_articles(corpus):
    """THE bug. reg.139 is the article that answers q20711, and it was not in the prompt."""
    got = numbers(select_articles(MARKINGS, DASHED_LINE, *corpus))
    assert "reg.139" in got, (
        f"'Strisce di separazione dei sensi di marcia' was not shown to the model: {got}")


def test_the_long_sign_article_no_longer_eats_the_budget(corpus):
    """reg.135 is 16,208 characters — two thirds of the whole budget on its own."""
    chosen = select_articles(MARKINGS, DASHED_LINE, *corpus)
    got = numbers(chosen)
    floor = [f"{s}.{n}" for s, n in articles_for(MARKINGS)]
    from_floor = [a for a in got if a in floor]
    assert len(from_floor) >= 4, f"only {len(from_floor)} of the topic's own articles: {got}"


def test_the_floor_gets_its_reserved_share(corpus):
    chosen = select_articles(MARKINGS, DASHED_LINE, *corpus)
    floor = {(s, n) for s, n in articles_for(MARKINGS)}
    spent = sum(len(a["text"]) for a in chosen if (a["source"], a["number"]) in floor)
    assert spent >= CONTEXT_CHARS * FLOOR_SHARE * 0.6, (
        f"the topic's articles got only {spent} of a "
        f"{int(CONTEXT_CHARS * FLOOR_SHARE)}-char reservation")


# --- and the sign path still works ------------------------------------------

SIGNS = "Segnali di divieto"
STOP = ["Il segnale raffigurato vieta il transito a tutti i veicoli",
        "Il segnale raffigurato è un segnale di divieto"]


def test_a_sign_cluster_still_gets_its_sign_articles(corpus):
    """The reservation must not break the path STATUS §12 says was hard-won: guessing the
    sign from the wording was measured wrong on 2 of 3 clusters, so the article naming the
    sign has to be there."""
    got = numbers(select_articles(SIGNS, STOP, *corpus))
    assert got, "a sign cluster was sent no statute at all"
    floor = {f"{s}.{n}" for s, n in articles_for(SIGNS)}
    assert set(got) & floor, f"none of the topic's own articles were chosen: {got}"


def test_sign_matches_are_still_included(corpus):
    """Half the budget is reserved, not all of it — a matched article OUTSIDE the topic's
    mapping must still get in, or the reservation has thrown away the ordering intent.

    """
    statements = DASHED_LINE
    matched = {tuple(r) for r in signs_in(statements, corpus[1])}
    floor = {(s, n) for s, n in articles_for(MARKINGS)}
    outside = matched - floor
    assert outside, "the fixture no longer produces an outside-the-floor match"

    chosen = {(a["source"], a["number"])
              for a in select_articles(MARKINGS, statements, *corpus)}
    assert chosen & outside, (
        f"matched articles outside the topic mapping were squeezed out: "
        f"wanted any of {outside}")


# --- the invariants that must survive ---------------------------------------

def test_the_budget_is_still_respected(corpus):
    for topic, statements in ((MARKINGS, DASHED_LINE), (SIGNS, STOP)):
        chosen = select_articles(topic, statements, *corpus)
        total = sum(len(a["text"]) for a in chosen)
        assert total <= CONTEXT_CHARS, f"{topic}: {total} chars exceeds the budget"


def test_no_article_is_sent_twice(corpus):
    """An article can be both a sign match and in the topic's mapping. Sending it twice
    wastes budget the floor needs."""
    chosen = select_articles(MARKINGS, DASHED_LINE, *corpus)
    keys = [(a["source"], a["number"]) for a in chosen]
    assert len(keys) == len(set(keys)), f"duplicates: {keys}"


def test_a_cluster_is_never_sent_bare(corpus):
    """A single article larger than its allowance is still better than none — the original
    behaviour, and the reason `generate` can return "no-article" rather than silently
    asking the model to invent one."""
    tiny = select_articles(MARKINGS, DASHED_LINE, corpus[0], corpus[1], budget=10)
    assert len(tiny) >= 1
