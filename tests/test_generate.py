"""The quality gates, which are the whole point of generate.py.

No API call is exercised here. What is exercised is the part that decides whether a
generated explanation reaches a human as `draft` or as `flagged` — and the check
that turns the ministerial answer key into a test of the model rather than an input
to it.
"""

from __future__ import annotations

import pytest

from generate import NUMERIC_RE, check, is_fatal, sample_statements

from shared.constants import STATUS_DRAFT, STATUS_FLAGGED


def member(qid, answer, statement="un'affermazione"):
    return {"id": qid, "answer": answer, "statement": statement}


def verdicts(*answers):
    return [{"n": i, "risposta": a, "certezza": "alta"} for i, a in enumerate(answers, 1)]


# --- the answer-key gate ---------------------------------------------------

def test_agreement_with_the_stored_key_passes_as_a_draft():
    judged = [member(1, True), member(2, False)]
    parsed = {"spiegazione": "Il segnale vieta il transito a tutti i veicoli.",
              "verdetti": verdicts("VERO", "FALSO")}
    status, reasons, disagreements = check(parsed, judged)
    assert status == STATUS_DRAFT
    assert reasons == [] and disagreements == []


def test_arguing_against_the_stored_answer_is_flagged():
    """Plan §3.3's second gate. It catches a model reasoning badly, a topic mapped
    to the wrong article, and an answer key that extraction got wrong — and a human
    has to say which."""
    judged = [member(1, True, "In presenza della luce rossa i veicoli devono arrestarsi")]
    parsed = {"spiegazione": "La luce rossa consente di proseguire con prudenza.",
              "verdetti": verdicts("FALSO")}
    status, reasons, disagreements = check(parsed, judged)
    assert status == STATUS_FLAGGED
    assert "argues against the stored answer" in reasons[0]
    assert disagreements == [{
        "question_id": 1, "stored": "VERO", "model": "FALSO",
        "statement": "In presenza della luce rossa i veicoli devono arrestarsi",
    }]


def test_a_missing_verdict_is_flagged_rather_than_assumed_to_agree():
    judged = [member(1, True), member(2, False)]
    parsed = {"spiegazione": "Una regola.", "verdetti": verdicts("VERO")}
    status, reasons, _ = check(parsed, judged)
    assert status == STATUS_FLAGGED
    assert "no verdict for statement 2" in reasons


def test_a_malformed_verdict_is_not_read_as_agreement():
    judged = [member(1, True)]
    parsed = {"spiegazione": "Una regola.", "verdetti": [{"n": 1, "risposta": "forse"}]}
    status, _, _ = check(parsed, judged)
    assert status == STATUS_FLAGGED


# --- the numeric gate ------------------------------------------------------

def test_an_explanation_carrying_a_number_is_flagged():
    judged = [member(1, True)]
    parsed = {"spiegazione": "Il limite fuori dai centri abitati è di 90 km/h.",
              "verdetti": verdicts("VERO")}
    status, reasons, _ = check(parsed, judged)
    assert status == STATUS_FLAGGED
    assert "contains a number or a unit" in reasons


def test_an_explanation_with_no_quantity_at_all_passes():
    judged = [member(1, True)]
    parsed = {"spiegazione": "La sospensione della patente è disposta dal prefetto.",
              "verdetti": verdicts("VERO")}
    status, reasons, _ = check(parsed, judged)
    assert status == STATUS_DRAFT, reasons


def test_the_mandatory_article_citation_does_not_trip_the_numeric_gate():
    """Observed on the first real run. The prompt requires a citation, citations
    contain digits, so leaving them in flagged every explanation ever generated — and
    a gate that fires on 100% of rows is one the reviewer learns to ignore."""
    judged = [member(1, True)]
    parsed = {"spiegazione": "Il segnale FERMARSI E DARE PRECEDENZA (art. 107 Reg.) "
                             "obbliga i conducenti a fermarsi e dare la precedenza "
                             "(art. 105 Reg.).",
              "verdetti": verdicts("VERO")}
    status, reasons, _ = check(parsed, judged)
    assert status == STATUS_DRAFT, reasons


@pytest.mark.parametrize("citation", [
    "(art. 106 Reg.)", "art. 148 C.d.S.", "articolo 142, comma 1 del codice",
    "art. 126-bis", "(fig. II.46)",
])
def test_citation_forms_are_all_stripped_before_the_numeric_check(citation):
    judged = [member(1, True)]
    parsed = {"spiegazione": f"Il conducente deve dare la precedenza {citation}.",
              "verdetti": verdicts("VERO")}
    status, reasons, _ = check(parsed, judged)
    assert status == STATUS_DRAFT, f"{citation} leaked into the numeric gate: {reasons}"


def test_a_real_quantity_beside_a_citation_is_still_caught():
    """The true positive from the same run: art. 106's 25 m / 10 m placement
    distances are exactly the kind of claim that has to be read against the text."""
    judged = [member(1, True)]
    parsed = {"spiegazione": "Il segnale va posto a non più di 25 m fuori dai centri "
                             "abitati e 10 m dentro (art. 106 Reg.).",
              "verdetti": verdicts("VERO")}
    status, reasons, _ = check(parsed, judged)
    assert status == STATUS_FLAGGED
    assert "contains a number or a unit" in reasons


def test_units_are_caught_without_a_digit():
    """Deliberately over-inclusive: plan §3.3 lists "anni" alongside km/h, so a
    duration counts as a numeric claim even spelled out. A false flag costs a glance
    at a row the reviewer is opening anyway; a missed one ships a wrong limit."""
    assert NUMERIC_RE.search("una distanza in metri")
    assert NUMERIC_RE.search("il tasso in g/l")
    assert NUMERIC_RE.search("la sospensione dura alcuni mesi")
    assert not NUMERIC_RE.search("il conducente deve rallentare")


def test_low_confidence_is_flagged_even_when_everything_agrees():
    judged = [member(1, True)]
    parsed = {"spiegazione": "Una regola generale.",
              "verdetti": [{"n": 1, "risposta": "VERO", "certezza": "bassa"}]}
    status, reasons, _ = check(parsed, judged)
    assert status == STATUS_FLAGGED
    assert "model reports low confidence" in reasons


# --- statement sampling ----------------------------------------------------

def test_a_small_cluster_is_judged_in_full():
    members = [member(i, i % 2 == 0) for i in range(5)]
    assert len(sample_statements(members, 12)) == 5


def test_a_large_cluster_keeps_both_answers_represented():
    """Judging only the VERO statements would miss exactly the failure this gate
    exists to catch: a model that agrees with whatever it is shown."""
    members = [member(i, True) for i in range(20)] + [member(100 + i, False) for i in range(20)]
    sampled = sample_statements(members, 8)
    assert len(sampled) == 8
    assert sum(1 for m in sampled if m["answer"]) == 4
    assert sum(1 for m in sampled if not m["answer"]) == 4


def test_sampling_is_deterministic_and_ordered_by_statement_number():
    members = [member(i, i % 3 == 0) for i in range(40)]
    first = sample_statements(members, 10)
    assert first == sample_statements(members, 10)
    assert [m["id"] for m in first] == sorted(m["id"] for m in first)


def test_an_all_one_answer_cluster_still_fills_the_sample():
    members = [member(i, True) for i in range(30)]
    assert len(sample_statements(members, 12)) == 12


# --- knowing when to stop --------------------------------------------------

class AuthenticationError(Exception):
    """Stands in for openai.AuthenticationError without importing the SDK."""


class RateLimitError(Exception):
    pass


def test_a_bad_key_stops_the_run_instead_of_repeating_itself():
    """Observed for real: a 401 was retried once per cluster. On the full bank that
    is 3382 identical failures burying the one line that explains them."""
    assert is_fatal(AuthenticationError("Incorrect API key provided: sk-proj-…"))
    assert is_fatal(Exception("Error code: 401 - {'code': 'invalid_api_key'}"))


def test_an_exhausted_quota_and_an_unreachable_model_are_also_terminal():
    assert is_fatal(Exception("insufficient_quota: You exceeded your current quota"))
    assert is_fatal(Exception("The model `gpt-9` does not exist or you do not have "
                              "access to it"))


def test_a_transient_failure_is_skipped_rather_than_fatal():
    """The cluster has no explanation row, so the next run simply picks it up."""
    assert not is_fatal(RateLimitError("Rate limit reached, please retry"))
    assert not is_fatal(TimeoutError("Request timed out"))
    assert not is_fatal(ValueError("Expecting value: line 1 column 1"))
