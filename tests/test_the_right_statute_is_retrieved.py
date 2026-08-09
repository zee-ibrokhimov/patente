"""The model is shown the law that settles THIS cluster, not the law listed first.

Only 43 of 3,382 clusters had an explanation. The cause was not cost, coverage or the
paywall: "Why?" generates on demand and pays for the call. The model was REFUSING —
`declined`, "articles insufficient" — because it is told to explain from the statute it is
handed and not from memory, and the statute it was handed did not contain the rule.

Two mechanisms, both fixed here:

  1. ORDER. `articles_for` returns a topic's articles in the order somebody mapped them,
     and 17 of the 25 topics hold more statute than the prompt budget — "Segnali di
     indicazione" is 100,844 characters against 24,000. So a fixed prefix of the topic
     answered every cluster in it, whatever the cluster was about.

  2. REACH. The mappings are hand-written and incomplete. Cluster 233 asks whether
     macchine agricole may drive on the road; `cds.57 "Macchine agricole"` sits in the
     corpus and is mapped to no topic that cluster belongs to. Ranked across the whole
     corpus it comes second.

And one trap found while fixing them: getting the ORDER right is not enough if first place
can spend the whole budget. Presence-scoring favours long articles because a longer text
contains more distinct words, so `cds.41 "Segnali luminosi"` (9,944 chars) outranked
`reg.139` (3,500) on incidental overlap and left no room for it inside the reservation.
Hence the length normalisation.
"""

from __future__ import annotations

import pytest

from api.services.articles import articles_for
from api.services.explanations import (
    CONTEXT_CHARS,
    corpus_and_index,
    rank_for,
    select_articles,
)


@pytest.fixture(scope="module")
def corpus():
    return corpus_and_index()


def refs(chosen) -> list[str]:
    return [f"{a['source']}.{a['number']}" for a in chosen]


# The real ministerial statements, verbatim. Cluster 638 is a dashed white line; 20711 is
# the question whose stored answer the old explanation contradicted.
MARKINGS = "Segnaletica orizzontale; segni sugli ostacoli"
DASHED = [
    "La striscia bianca discontinua in figura può essere superata",
    "La striscia bianca discontinua in figura divide la carreggiata in due corsie",
    "Nelle strade a doppio senso con due corsie la striscia bianca discontinua in figura "
    "divide i sensi di marcia",
    "La striscia bianca discontinua in figura consente l'inversione di marcia, in "
    "condizioni di sicurezza, se la strada è a doppio senso",
    "La striscia bianca discontinua in figura divide la strada da una pista ciclabile",
]

# Cluster 233. Its topic maps neither cds.57 nor cds.104 nor cds.114.
# The exact ministerial topic name — 274 characters of it. Abbreviating it means
# `articles_for` matches nothing and the test silently exercises a different path.
DEFINITIONS = "Definizioni stradali e di traffico; definizioni e classificazione dei veicoli; doveri del conducente nell'uso della strada - convivenza civile ed uso responsabile della strada; riguardo verso gli utenti deboli della strada (anziani, diversamente abili, bambini, pedoni, ciclisti)"
FARM = [
    "Le macchine agricole possono circolare su strada per il proprio trasferimento se "
    "immatricolate",
]


# --- ranking ----------------------------------------------------------------

def test_the_answering_article_outranks_the_first_mapped_one(corpus):
    """reg.139 names what a dashed line separates. In mapping order it was third."""
    ranked = rank_for(articles_for(MARKINGS), DASHED, corpus[0])
    top = [f"{s}.{n}" for s, n in ranked[:3]]
    assert "reg.139" in top, f"the article that settles the cluster is not near the top: {top}"


def test_a_long_article_does_not_win_on_incidental_overlap(corpus):
    """THE trap. cds.41 "Segnali luminosi" is 9,944 characters and about traffic LIGHTS.
    Without length normalisation it outscored reg.139 on shared common words, took 9,944
    of a 12,000-character reservation, and pushed the answering article out entirely."""
    ranked = rank_for(articles_for(MARKINGS), DASHED, corpus[0])
    order = [f"{s}.{n}" for s, n in ranked]
    assert order.index("reg.139") < order.index("cds.41"), (
        f"a long off-topic article outranks the one that answers the cluster: {order[:6]}")


def test_ranking_is_stable_when_nothing_discriminates(corpus):
    """No content words means no signal, and the hand-mapped order is what to fall back to
    — not an arbitrary reshuffle."""
    mapped = articles_for(MARKINGS)
    assert rank_for(mapped, ["di e a il la"], corpus[0])[:5] == list(mapped)[:5]


# --- selection --------------------------------------------------------------

def test_the_answering_article_reaches_the_prompt(corpus):
    """Ranking it first is worthless if the budget is spent before it is reached."""
    assert "reg.139" in refs(select_articles(MARKINGS, DASHED, *corpus))


def test_an_unmapped_article_can_still_be_found(corpus):
    """Cluster 233 declined because the law it needed was mapped to no topic it is in.
    Corpus-wide ranking is what reaches it, and it is the difference between that cluster
    having an explanation and never having one."""
    got = refs(select_articles(DEFINITIONS, FARM, *corpus))
    assert "cds.57" in got, f"cds.57 'Macchine agricole' was not retrieved: {got}"


def test_the_topic_still_gets_its_reserved_share(corpus):
    """Widening the search must not let an off-topic article with shared vocabulary
    displace the topic's own statute — the floor is why no cluster is ever sent bare."""
    chosen = select_articles(MARKINGS, DASHED, *corpus)
    floor = {(s, n) for s, n in articles_for(MARKINGS)}
    from_floor = [a for a in chosen if (a["source"], a["number"]) in floor]
    assert len(from_floor) >= 4, f"only {len(from_floor)} of the topic's own: {refs(chosen)}"


def test_the_budget_is_still_respected(corpus):
    for topic, statements in ((MARKINGS, DASHED), (DEFINITIONS, FARM)):
        chosen = select_articles(topic, statements, *corpus)
        total = sum(len(a["text"]) for a in chosen)
        assert total <= CONTEXT_CHARS, f"{topic}: {total} exceeds {CONTEXT_CHARS}"


def test_nothing_is_sent_twice(corpus):
    """An article can now arrive by three routes — floor, sign match, corpus-wide."""
    keys = [(a["source"], a["number"]) for a in select_articles(MARKINGS, DASHED, *corpus)]
    assert len(keys) == len(set(keys)), f"duplicates: {keys}"
