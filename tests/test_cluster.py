"""Clustering rules.

The pure parts, which is where the correctness lives — the rest is a similarity
matrix and a report.
"""

import pytest

from cluster import (
    SPLIT_BY_QUESITO,
    build_clusters,
    cluster_by_text,
    connected_components,
    needs_split_review,
    normalise,
    persist,
)

from api.models import Cluster, Explanation, Question, Quesito, Topic


def row(qid, topic_id, statement, image=None, topic="T", quesito=None):
    return {"id": qid, "topic_id": topic_id, "statement": statement,
            "image": image, "answer": True, "topic": topic,
            "quesito": quesito if quesito is not None else qid}


# --- normalisation ---------------------------------------------------------

def test_accents_and_case_are_folded_for_comparison():
    assert normalise("La velocità È Alta") == normalise("la velocita e alta")


def test_punctuation_is_dropped():
    assert normalise("velocità, massima: 50 km/h") == normalise("velocita massima 50 km h")


def test_articles_and_prepositions_are_dropped():
    """They carry no rule, and keeping them inflates the similarity of unrelated
    statements that happen to share Italian grammar."""
    assert "il" not in normalise("il segnale").split()
    assert "della" not in normalise("la fine della strada").split()


def test_normalisation_does_not_mutate_the_stored_text():
    original = "Il segnale raffigurato è di pericolo"
    normalise(original)
    assert original == "Il segnale raffigurato è di pericolo"


# --- components ------------------------------------------------------------

def test_isolated_items_each_form_their_own_component():
    assert sorted(len(c) for c in connected_components(3, [])) == [1, 1, 1]


def test_similarity_chains_transitively():
    """A~B and B~C pulls A and C together even if they are unalike. This is a
    property of the algorithm, not a bug — the threshold is what keeps chains
    short, and the report prints the largest clusters so it stays visible."""
    components = connected_components(3, [(0, 1), (1, 2)])
    assert len(components) == 1 and len(components[0]) == 3


# --- clustering ------------------------------------------------------------

def test_reworded_statements_cluster_together():
    statements = [
        "Il segnale raffigurato vieta il transito ai pedoni",
        "Il segnale in figura vieta il transito ai pedoni",
        "La distanza di sicurezza dipende dalla velocità",
    ]
    clusters = cluster_by_text(statements, threshold=75)
    sizes = sorted(len(c) for c in clusters)
    assert sizes == [1, 2]


def test_text_similarity_misses_the_raffigurato_in_figura_rewording():
    """The measured limit of the text strategy, and the case for the figure one.

    "Il segnale raffigurato ..." and "Il segnale in figura ..." are the same rule
    in ministerial phrasing and score 77.9 — under the default threshold of 88
    they stay apart. Both statements carry the same figure, so grouping sign
    questions by figure catches what wording comparison cannot.
    """
    pair = [
        "Il segnale raffigurato vieta il transito ai pedoni",
        "Il segnale in figura vieta il transito ai pedoni",
    ]
    assert len(cluster_by_text(pair, threshold=88)) == 2

    rows = [row(1, 1, pair[0], image="images/a.jpeg"),
            row(2, 1, pair[1], image="images/a.jpeg")]
    assert len(build_clusters(rows, strategy="figure", threshold=88)) == 1


def test_unrelated_statements_stay_apart():
    statements = [
        "Il segnale raffigurato vieta il transito",
        "La patente di guida va rinnovata ogni dieci anni",
    ]
    assert len(cluster_by_text(statements, threshold=88)) == 2


def test_a_single_statement_is_a_valid_cluster():
    assert cluster_by_text(["solo questo"], threshold=88) == [[0]]


def test_empty_input_produces_no_clusters():
    assert cluster_by_text([], threshold=88) == []


# --- topic boundary --------------------------------------------------------

def test_topics_are_never_merged():
    """Explanations key off cluster_id and topics go live one at a time, so a
    cluster spanning topics would block both from ever shipping independently."""
    identical = "Il segnale raffigurato vieta il transito"
    rows = [row(1, 10, identical), row(2, 20, identical)]
    clusters = build_clusters(rows, strategy="text", threshold=70)

    assert len(clusters) == 2
    for members in clusters.values():
        assert len({m["topic_id"] for m in members}) == 1


# --- figure strategy -------------------------------------------------------

def test_figure_strategy_groups_by_figure_regardless_of_wording():
    """Statements about one sign are about one rule, however differently phrased."""
    rows = [
        row(1, 1, "Il segnale raffigurato vieta il transito", image="images/a.jpeg"),
        row(2, 1, "La figura rappresenta un segnale di pericolo", image="images/a.jpeg"),
        row(3, 1, "Il segnale raffigurato indica un parcheggio", image="images/b.jpeg"),
    ]
    clusters = build_clusters(rows, strategy="figure", threshold=88)
    by_size = sorted((sorted(m["id"] for m in v) for v in clusters.values()), key=len)
    assert by_size == [[3], [1, 2]]


def test_composite_figures_form_their_own_cluster():
    """"Il segnale (A) ... il segnale (B)" ships a different image, so it must not
    inherit the explanation written for the plain sign."""
    rows = [
        row(1, 1, "Il segnale raffigurato vieta il transito", image="images/a.jpeg"),
        row(2, 1, "Il segnale (A) segue il segnale (B)", image="images/ab.jpeg"),
    ]
    clusters = build_clusters(rows, strategy="figure", threshold=88)
    assert len(clusters) == 2


def test_text_only_statements_still_cluster_under_the_figure_strategy():
    rows = [
        row(1, 1, "Il segnale raffigurato vieta il transito", image="images/a.jpeg"),
        row(2, 1, "La distanza di sicurezza dipende dalla velocità"),
        row(3, 1, "La distanza di sicurezza dipende dalla velocita"),
    ]
    clusters = build_clusters(rows, strategy="figure", threshold=85)
    assert sorted(len(v) for v in clusters.values()) == [1, 2]


def test_every_statement_lands_in_exactly_one_cluster():
    rows = [row(i, i % 3, f"statement number {i}") for i in range(30)]
    clusters = build_clusters(rows, strategy="text", threshold=88)
    assigned = [m["id"] for members in clusters.values() for m in members]
    assert sorted(assigned) == sorted(r["id"] for r in rows)
    assert len(assigned) == len(set(assigned))


@pytest.mark.parametrize("strategy", ["text", "figure"])
def test_no_statement_is_lost_by_either_strategy(strategy):
    rows = [row(i, i % 4, f"il veicolo {i} deve procedere",
                image="images/a.jpeg" if i % 2 else None) for i in range(40)]
    clusters = build_clusters(rows, strategy=strategy, threshold=88)
    total = sum(len(v) for v in clusters.values())
    assert total == len(rows)


# --- hand-listed splits ----------------------------------------------------

def test_a_listed_figure_cluster_splits_along_the_ministerial_quesito():
    """One traffic-light figure carries four different rules. No text statistic
    separates that case from a sign asked thirty ways, so the split is a hand
    list and the division is the Ministry's own grouping."""
    topic_id, image = next(iter(SPLIT_BY_QUESITO))
    rows = [
        row(1, topic_id, "La luce rossa impone l'arresto", image=image, quesito=10),
        row(2, topic_id, "La luce rossa vieta di proseguire", image=image, quesito=10),
        row(3, topic_id, "La luce verde consente di proseguire", image=image, quesito=11),
    ]
    clusters = build_clusters(rows, strategy="figure", threshold=88)
    assert sorted(len(v) for v in clusters.values()) == [1, 2]


def test_an_unlisted_figure_cluster_is_never_split_by_size():
    """The 34-member DIVIETO DI SORPASSO cluster is one rule asked 34 ways.
    Splitting it would mean explaining that rule several times and correcting it
    in several places — the thing clustering exists to prevent."""
    rows = [row(i, 99, f"Il segnale raffigurato vieta il sorpasso, caso {i}",
                image="images/sorpasso.jpeg", quesito=i) for i in range(40)]
    clusters = build_clusters(rows, strategy="figure", threshold=88)
    assert len(clusters) == 1


def test_the_review_flag_is_advisory_and_does_not_split():
    big = [row(i, 1, f"statement {i}", image="images/x.jpeg", quesito=1) for i in range(25)]
    small = [row(i, 1, f"statement {i}", image="images/y.jpeg", quesito=1) for i in range(3)]
    assert needs_split_review(big)
    assert not needs_split_review(small)
    assert len(build_clusters(big, strategy="figure", threshold=88)) == 1


# --- cluster identity survives a rerun -------------------------------------

def seed_two_questions(session):
    session.add(Topic(id=1, name="Segnali di divieto"))
    session.flush()
    session.add(Quesito(id=1, topic_id=1, primary_image=None))
    session.flush()
    session.add_all([
        Question(id=1, quesito_id=1, topic_id=1, statement_it="Il segnale vieta il transito",
                 answer=True, image_path=None, source_version="v1"),
        Question(id=2, quesito_id=1, topic_id=1, statement_it="La distanza dipende dalla velocità",
                 answer=True, image_path=None, source_version="v1"),
    ])
    session.flush()
    return [row(1, 1, "Il segnale vieta il transito", quesito=1),
            row(2, 1, "La distanza dipende dalla velocità", quesito=1)]


def test_rerunning_keeps_cluster_ids_and_their_explanations(session):
    """The bug this guards: cluster ids used to be positional, `persist()` deleted
    every row before rewriting, and `explanations.cluster_id` is ON DELETE CASCADE
    under `PRAGMA foreign_keys=ON`. Re-running the clustering step after
    generating explanations therefore destroyed all of them, and said nothing."""
    rows = seed_two_questions(session)
    clusters = build_clusters(rows, strategy="text", threshold=88)
    persist(session, clusters)
    session.flush()

    approved = session.query(Cluster).order_by(Cluster.id).first()
    session.add(Explanation(cluster_id=approved.id, lang="it",
                            text="Il segnale vieta il transito a tutti i veicoli.",
                            status="approved"))
    session.flush()
    keyed = {c.natural_key: c.id for c in session.query(Cluster)}

    # Same input, run again — as would happen after any reseed.
    stats = persist(session, build_clusters(rows, strategy="text", threshold=88))
    session.flush()

    assert stats["removed"] == 0 and stats["new"] == 0
    assert {c.natural_key: c.id for c in session.query(Cluster)} == keyed
    assert session.query(Explanation).count() == 1


def test_a_rule_that_leaves_the_listato_takes_its_cluster_with_it(session):
    """The opposite case, which must still work: a cluster whose rule is gone is
    deleted, explanations included, because it now explains nothing."""
    rows = seed_two_questions(session)
    persist(session, build_clusters(rows, strategy="text", threshold=88))
    session.flush()
    assert session.query(Cluster).count() == 2

    stats = persist(session, build_clusters(rows[:1], strategy="text", threshold=88))
    session.flush()
    assert stats["removed"] == 1
    assert session.query(Cluster).count() == 1
