"""Seeding behaviour under a reissued listato.

The Ministry reissues the listato periodically. These cover what must survive a
reseed and what must not.
"""

import copy

import pytest
from sqlalchemy import select

from api.models import Cluster, Figure, Question, Topic
from seed import seed


def run(session, data, figure_root, **kw):
    return seed(session, data, figure_root=figure_root, **kw)


def test_first_seed_loads_everything(session, listato, figure_root):
    stats, retired, recluster = run(session, listato, figure_root)
    assert stats["topic_new"] == 2
    assert stats["figure_new"] == 2
    assert stats["quesito_new"] == 2
    assert stats["question_new"] == 4
    assert retired == [] and recluster == []

    q = session.get(Question, 2)
    assert q.image_path == "images/sign_b.jpeg"  # composite figure, not the group's
    assert q.answer is False


def test_reseeding_identical_data_changes_nothing(session, listato, figure_root):
    run(session, listato, figure_root)
    session.commit()

    stats, retired, recluster = run(session, copy.deepcopy(listato), figure_root)
    assert stats["question_unchanged"] == 4
    assert stats["question_new"] == 0
    assert stats["question_rewritten"] == 0
    assert retired == [] and recluster == []


def test_telegram_file_id_survives_a_reseed(session, listato, figure_root):
    """Losing this would mean re-uploading every sign image to every user."""
    run(session, listato, figure_root)
    session.get(Figure, "images/sign_a.jpeg").telegram_file_id = "AgACAgIAAxk"
    session.commit()

    run(session, copy.deepcopy(listato), figure_root)
    session.commit()
    assert session.get(Figure, "images/sign_a.jpeg").telegram_file_id == "AgACAgIAAxk"


def test_changed_statement_detaches_its_cluster(session, listato, figure_root):
    """An explanation written for the old wording must not survive a rewording."""
    run(session, listato, figure_root)
    session.add(Cluster(id=1, rule_summary="divieto di transito"))
    session.flush()
    session.get(Question, 1).cluster_id = 1
    session.commit()

    reissued = copy.deepcopy(listato)
    reissued["questions"][0]["statement_it"] = "Il segnale raffigurato vieta il transito ai pedoni"
    stats, _retired, recluster = run(session, reissued, figure_root)

    assert stats["question_rewritten"] == 1
    assert recluster == [1]
    assert session.get(Question, 1).cluster_id is None


def test_flipped_answer_detaches_its_cluster(session, listato, figure_root):
    """The severe case: the stored explanation now argues the opposite of the key."""
    run(session, listato, figure_root)
    session.add(Cluster(id=1, rule_summary="distanza"))
    session.flush()
    session.get(Question, 3).cluster_id = 1
    session.commit()

    reissued = copy.deepcopy(listato)
    reissued["questions"][2]["answer"] = False
    stats, _retired, recluster = run(session, reissued, figure_root)

    assert recluster == [3]
    assert session.get(Question, 3).answer is False
    assert session.get(Question, 3).cluster_id is None


def test_metadata_only_change_keeps_the_cluster(session, listato, figure_root):
    """Re-filing a question under another topic does not invalidate its explanation."""
    run(session, listato, figure_root)
    session.add(Cluster(id=1, rule_summary="divieto"))
    session.flush()
    session.get(Question, 1).cluster_id = 1
    session.commit()

    reissued = copy.deepcopy(listato)
    reissued["questions"][0]["topic"] = "Distanza di sicurezza"
    stats, _retired, recluster = run(session, reissued, figure_root)

    assert stats["question_metadata"] == 1
    assert stats["question_rewritten"] == 0
    assert recluster == []
    assert session.get(Question, 1).cluster_id == 1


def test_retired_questions_are_reported_not_deleted(session, listato, figure_root):
    """Users have progress rows pointing at them."""
    run(session, listato, figure_root)
    session.commit()

    reissued = copy.deepcopy(listato)
    reissued["questions"] = [q for q in reissued["questions"] if q["id"] != 4]
    stats, retired, _recluster = run(session, reissued, figure_root)

    assert retired == [4]
    assert stats.get("question_pruned", 0) == 0
    assert session.get(Question, 4) is not None


def test_prune_removes_retired_questions(session, listato, figure_root):
    run(session, listato, figure_root)
    session.commit()

    reissued = copy.deepcopy(listato)
    reissued["questions"] = [q for q in reissued["questions"] if q["id"] != 4]
    stats, retired, _ = run(session, reissued, figure_root, prune=True)

    assert retired == [4] and stats["question_pruned"] == 1
    assert session.get(Question, 4) is None


def test_topic_ids_are_stable_when_a_topic_is_inserted(session, listato, figure_root):
    """extract.py numbers topics alphabetically, so a new topic renumbers the rest.

    Seeding keys on the name instead; otherwise one new ministerial topic would
    silently re-point thousands of questions at the wrong subject.
    """
    run(session, listato, figure_root)
    session.commit()
    before = {t.name: t.id for t in session.scalars(select(Topic))}

    reissued = copy.deepcopy(listato)
    # "Circolazione" sorts first, taking id 1 from "Distanza di sicurezza".
    reissued["topics"] = [
        {"id": 1, "name": "Circolazione"},
        {"id": 2, "name": "Distanza di sicurezza"},
        {"id": 3, "name": "Segnali di divieto"},
    ]
    for q in reissued["questions"]:
        q["topic_id"] = {"Segnali di divieto": 3, "Distanza di sicurezza": 2}[q["topic"]]
    reissued["quesiti"][0]["topic_id"] = 3
    reissued["quesiti"][1]["topic_id"] = 2
    run(session, reissued, figure_root)
    session.commit()

    after = {t.name: t.id for t in session.scalars(select(Topic))}
    assert after["Segnali di divieto"] == before["Segnali di divieto"]
    assert after["Distanza di sicurezza"] == before["Distanza di sicurezza"]
    assert session.get(Question, 1).topic_id == before["Segnali di divieto"]


def test_missing_figure_file_aborts_the_seed(session, listato, figure_root):
    """questions.json must never reference a figure that is not on disk."""
    (figure_root / "images" / "sign_b.jpeg").unlink()
    with pytest.raises(SystemExit, match="not on disk"):
        run(session, listato, figure_root)
