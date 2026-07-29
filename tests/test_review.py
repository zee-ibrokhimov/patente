"""The review loop — the only path by which anything becomes `approved`.

`approved` is the only status the API serves, so every mistake here ships. The tests
are about what the importer *refuses*: a decision it does not recognise, an edit with
no text, and above all a draft that changed after the sheet was exported.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from review_export import collect, fingerprint, render_statements
from review_import import apply_row

from api.models import Cluster, Explanation, Question, Quesito, Topic
from shared.constants import STATUS_APPROVED, STATUS_DRAFT, STATUS_FLAGGED, STATUS_REJECTED

NOW = datetime(2026, 7, 29, 21, 0, tzinfo=timezone.utc)
DRAFT = "Il segnale vieta il transito a tutti i veicoli (art. 116 Reg.)."


def draft(text: str = DRAFT, status: str = STATUS_FLAGGED) -> Explanation:
    return Explanation(cluster_id=1, lang="it", text=text, status=status,
                       flags="contains a number or a unit")


def row(**overrides) -> dict:
    base = {"natural_key": "t1|txt:1", "fingerprint": fingerprint(DRAFT),
            "decision": "", "explanation_edited": "", "reviewer": "anna", "note": ""}
    return {**base, **overrides}


# --- decisions -------------------------------------------------------------

def test_approve_marks_it_approved_and_stamps_the_reviewer():
    explanation = draft()
    assert apply_row(row(decision="approve"), explanation, "anna", NOW) == "approve"
    assert explanation.status == STATUS_APPROVED
    assert explanation.reviewed_at == NOW
    assert explanation.reviewer == "anna"


def test_approving_clears_the_flags_a_human_has_now_ruled_on():
    explanation = draft()
    apply_row(row(decision="approve"), explanation, "anna", NOW)
    assert explanation.flags is None


def test_reject_marks_it_rejected_and_leaves_the_text_alone():
    explanation = draft()
    assert apply_row(row(decision="reject"), explanation, "anna", NOW) == "reject"
    assert explanation.status == STATUS_REJECTED
    assert explanation.text == DRAFT


def test_edit_replaces_the_text_and_counts_as_read():
    """Text somebody rewrote has by definition been read, so an edit approves."""
    explanation = draft()
    corrected = "Il segnale vieta il transito ai soli veicoli a motore (art. 116 Reg.)."
    apply_row(row(decision="edit", explanation_edited=corrected), explanation, "anna", NOW)
    assert explanation.text == corrected
    assert explanation.status == STATUS_APPROVED


def test_edit_with_nothing_written_is_refused():
    with pytest.raises(ValueError, match="explanation_edited is empty"):
        apply_row(row(decision="edit"), draft(), "anna", NOW)


def test_approve_with_an_edit_filled_in_is_refused_as_ambiguous():
    """Two contradictory instructions on one row. Guessing either way is worse than
    making the reviewer say which they meant."""
    with pytest.raises(ValueError, match="use 'edit'"):
        apply_row(row(decision="approve", explanation_edited="qualcosa di diverso"),
                  draft(), "anna", NOW)


def test_approve_tolerates_the_edit_column_holding_the_unchanged_text():
    """A spreadsheet that copied the draft across is not a contradiction."""
    explanation = draft()
    apply_row(row(decision="approve", explanation_edited=DRAFT), explanation, "anna", NOW)
    assert explanation.status == STATUS_APPROVED


@pytest.mark.parametrize("word", ["ok", "yes", "sì", "approvato", "y", "APPROVE?"])
def test_a_decision_it_does_not_recognise_is_refused_rather_than_guessed(word):
    """A typo in this column silently approving a row is the failure this file
    exists to prevent."""
    with pytest.raises(ValueError, match="unrecognised decision"):
        apply_row(row(decision=word), draft(), "anna", NOW)


@pytest.mark.parametrize("word", ["approve", "  Approve  ", "APPROVE"])
def test_case_and_whitespace_are_forgiven(word):
    explanation = draft()
    apply_row(row(decision=word), explanation, "anna", NOW)
    assert explanation.status == STATUS_APPROVED


# --- the fingerprint -------------------------------------------------------

def test_the_fingerprint_follows_the_text():
    assert fingerprint(DRAFT) == fingerprint(DRAFT)
    assert fingerprint(DRAFT) != fingerprint(DRAFT + " ")


def test_a_regenerated_draft_no_longer_matches_the_exported_fingerprint():
    """The whole point: export, re-run generate.py, import an approval, and you
    would approve a sentence nobody read."""
    exported = fingerprint(DRAFT)
    regenerated = draft(text="Una spiegazione completamente diversa.")
    assert fingerprint(regenerated.text) != exported


# --- the sheet -------------------------------------------------------------

def test_statements_are_rendered_with_their_answers():
    """A reviewer cannot judge "one explanation serves every variant" without
    seeing the variants."""
    rendered = render_statements([
        (2, "Il segnale vieta il transito", True),
        (1, "Il segnale indica un parcheggio", False),
    ])
    lines = rendered.splitlines()
    assert lines[0].startswith("FALSO") and "[1]" in lines[0]
    assert lines[1].startswith("VERO") and "[2]" in lines[1]


def test_the_sheet_carries_the_statements_and_a_matching_fingerprint(session):
    session.add(Topic(id=1, name="Segnali di divieto"))
    session.flush()
    session.add(Quesito(id=1, topic_id=1, primary_image=None))
    session.add(Cluster(id=1, natural_key="t1|fig:images/a.jpeg", topic_id=1,
                        rule_summary="divieto di transito"))
    session.flush()
    session.add_all([
        Question(id=1, quesito_id=1, topic_id=1, statement_it="Il segnale vieta il transito",
                 answer=True, image_path=None, cluster_id=1, source_version="v1"),
        Question(id=2, quesito_id=1, topic_id=1, statement_it="Il segnale indica un parcheggio",
                 answer=False, image_path=None, cluster_id=1, source_version="v1"),
    ])
    session.add(Explanation(cluster_id=1, lang="it", text=DRAFT, status=STATUS_DRAFT))
    session.flush()

    rows = collect(session, "it", None, [STATUS_DRAFT])
    assert len(rows) == 1
    sheet_row = rows[0]
    assert sheet_row["natural_key"] == "t1|fig:images/a.jpeg"
    assert sheet_row["n_statements"] == 2
    assert "Il segnale vieta il transito" in sheet_row["statements"]
    assert "Il segnale indica un parcheggio" in sheet_row["statements"]
    assert sheet_row["fingerprint"] == fingerprint(DRAFT)
    assert sheet_row["decision"] == ""


def test_approved_rows_are_left_out_of_the_working_set(session):
    session.add(Topic(id=1, name="Segnali di divieto"))
    session.flush()
    session.add(Cluster(id=1, natural_key="t1|txt:1", topic_id=1, rule_summary="x"))
    session.flush()
    session.add(Explanation(cluster_id=1, lang="it", text=DRAFT, status=STATUS_APPROVED))
    session.flush()

    assert collect(session, "it", None, [STATUS_DRAFT, STATUS_FLAGGED]) == []
    assert len(collect(session, "it", None, None)) == 1
