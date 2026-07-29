"""Clustering rules.

The pure parts, which is where the correctness lives — the rest is a similarity
matrix and a report.
"""

import pytest

from cluster import build_clusters, cluster_by_text, connected_components, normalise


def row(qid, topic_id, statement, image=None, topic="T"):
    return {"id": qid, "topic_id": topic_id, "statement": statement,
            "image": image, "answer": True, "topic": topic}


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
