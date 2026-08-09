"""Why "Объяснение для этого вопроса пока недоступно" came back, and what changed.

Reported by the owner: tapping "Why?" says the explanation is not available.

`Access.UNAVAILABLE` has four distinct causes and the learner sees one sentence for all of
them. Measured on live data, 22 clusters explained:

  · 2 clusters withheld entirely — BOTH by the numeric gate
  · 17 individual questions withheld because the model disputed the answer key on them
  · every cold cluster generating on demand, ~20-30% of which decline

THE NUMERIC GATE WAS THE BIGGEST AND THE LEAST JUSTIFIED

It banned any digit outside a citation. 9 of the 10 withheld rows were withheld by it,
including explanations that were quoting the article correctly. The fear is right — a wrong
speed limit is the worst thing this product can say — but "contains a digit" is not the same
property as "invented a figure", and the old rule could not tell them apart, so it withheld
both. It now checks GROUNDING: every number must appear in the statute the model was shown.

RETRYING A DECLINE

The module's own docstring says a decline is "partly run-to-run noise". That is the argument
for asking twice — a learner standing there having tapped "Why?" and being told to come back
later, for a cluster that would have answered on a second roll, is the most annoying way to
fail. One retry, not a loop: a cluster whose articles genuinely do not cover the statements
declines every time, and paying repeatedly to hear that is how a per-request cost becomes
unbounded.

WHAT DELIBERATELY STILL WITHHOLDS

A statement the model contradicted. That one is not a bug: a learner must never read an
explanation that argues against the answer they were just shown. It stays.
"""

from __future__ import annotations

import pytest

from api.services import explanations
from api.services.explanations import check_gates, ungrounded_numbers
from shared.constants import STATUS_DRAFT, STATUS_FLAGGED

ARTICLE = [{
    "source": "cds",
    "number": "142",
    "rubric": "Limiti di velocità",
    "text": "1. Ai fini della sicurezza della circolazione la velocità non può superare "
            "i 130 km/h per le autostrade, i 110 km/h per le strade extraurbane "
            "principali e i 50 km/h per le strade nei centri abitati. Il limite è di 3,5 "
            "tonnellate per i veicoli di cui all'articolo 54.",
}]


def judged(n: int = 2, answers=(True, False)) -> list[dict]:
    return [{"id": 100 + i, "statement": f"Affermazione {i}", "answer": answers[i % len(answers)]}
            for i in range(n)]


def reply(italian: str, verdicts=None) -> dict:
    return {
        "insufficiente": False,
        "spiegazione": {"it": italian, "ru": "…", "en": "…", "uz": "…"},
        "verdetti": verdicts or [
            {"n": 1, "risposta": "VERO", "certezza": "alta"},
            {"n": 2, "risposta": "FALSO", "certezza": "alta"},
        ],
    }


# --- the grounding rule ------------------------------------------------------

def test_a_number_quoted_from_the_article_is_grounded():
    """THE case that was being withheld. The article says 50 km/h; saying 50 km/h is what
    the feature is FOR."""
    assert ungrounded_numbers(
        "Nei centri abitati il limite è di 50 km/h (art. 142 C.d.S.).", ARTICLE) == []


def test_an_invented_number_is_caught():
    """The actual danger, and the reason the gate exists at all."""
    assert ungrounded_numbers(
        "Nei centri abitati il limite è di 70 km/h (art. 142 C.d.S.).", ARTICLE) == ["70"]


def test_a_decimal_is_matched_across_separators():
    """The corpus writes 3,5 and a model may write 3.5. A separator mismatch flagging a
    correctly quoted figure would reintroduce the whole problem."""
    assert ungrounded_numbers("Il limite è di 3.5 tonnellate.", ARTICLE) == []
    assert ungrounded_numbers("Il limite è di 3,5 tonnellate.", ARTICLE) == []


def test_text_with_no_numbers_passes():
    assert ungrounded_numbers("Il conducente deve dare la precedenza.", ARTICLE) == []


def test_the_citation_itself_is_never_counted():
    """The prompt REQUIRES a citation, so every draft contains one. Counting its digits
    made the gate fire on 100% of drafts, which is a gate a reviewer learns to ignore."""
    assert ungrounded_numbers("Una regola (art. 148 C.d.S.).", ARTICLE) == []
    assert ungrounded_numbers("Vedi fig. II. 4 e l'art. 99, comma 2.", ARTICLE) == []


def test_with_no_article_to_check_against_it_stays_blunt():
    """An unchecked number is the thing being guarded. Passing everything because there is
    nothing to compare with would be the wrong direction to fail in."""
    assert ungrounded_numbers("Il limite è di 50 km/h.", []) == ["50"]


def test_a_number_written_in_words_still_flags():
    """Deliberately strict: grounding is checked literally, so a correct figure that has
    been rephrased flags. Failing toward review is the right direction."""
    assert ungrounded_numbers("Il limite è di cinquanta km/h.", ARTICLE) == []
    assert ungrounded_numbers("Il limite è di 51 km/h.", ARTICLE) == ["51"]


# --- what that does to the gate ---------------------------------------------

def test_an_explanation_quoting_the_statute_is_now_served():
    status, reasons, _ = check_gates(
        reply("Nei centri abitati il limite è di 50 km/h (art. 142 C.d.S.)."),
        judged(), ARTICLE)
    assert status == STATUS_DRAFT, f"still withheld: {reasons}"


def test_an_explanation_inventing_a_figure_is_still_withheld():
    status, reasons, _ = check_gates(
        reply("Nei centri abitati il limite è di 70 km/h (art. 142 C.d.S.)."),
        judged(), ARTICLE)
    assert status == STATUS_FLAGGED
    assert any("not in the cited article" in r for r in reasons)
    assert any("70" in r for r in reasons)


def test_the_reason_names_the_number():
    """A reviewer needs to know WHICH figure to check. "contains a number" sent them to
    read the whole thing."""
    _status, reasons, _ = check_gates(
        reply("Il limite è 70 km/h e 90 km/h (art. 142 C.d.S.)."), judged(), ARTICLE)
    joined = " ".join(reasons)
    assert "70" in joined and "90" in joined


def test_low_confidence_still_withholds():
    status, reasons, _ = check_gates(
        reply("Una regola senza numeri.",
              verdicts=[{"n": 1, "risposta": "VERO", "certezza": "bassa"},
                        {"n": 2, "risposta": "FALSO", "certezza": "alta"}]),
        judged(), ARTICLE)
    assert status == STATUS_FLAGGED
    assert any("low confidence" in r for r in reasons)


def test_disputing_the_answer_key_still_withholds_that_statement():
    """Not a bug and not relaxed: a learner must never read an explanation arguing against
    the answer they were just shown."""
    _status, _reasons, disagreements = check_gates(
        reply("Una regola senza numeri.",
              verdicts=[{"n": 1, "risposta": "FALSO", "certezza": "alta"},
                        {"n": 2, "risposta": "FALSO", "certezza": "alta"}]),
        judged(), ARTICLE)
    assert [d["question_id"] for d in disagreements] == [100]


def test_disputing_most_of_the_cluster_still_withholds_all_of_it():
    _status, reasons, _ = check_gates(
        reply("Una regola.",
              verdicts=[{"n": 1, "risposta": "FALSO", "certezza": "alta"},
                        {"n": 2, "risposta": "VERO", "certezza": "alta"}]),
        judged(), ARTICLE)
    assert any("most of the cluster" in r for r in reasons)


# --- the retry ---------------------------------------------------------------

async def test_a_decline_is_retried_once(api_db, monkeypatch, tmp_path):
    """"Partly run-to-run noise" is the module's own words. A learner told to come back
    later for a cluster that would have answered on a second roll is the most annoying way
    to fail, because nothing was wrong."""
    from tests.test_explanation_service import FakeClient, REPLY

    calls = []
    client = FakeClient()

    async def create(**kwargs):
        calls.append(1)
        if len(calls) == 1:
            client.reply = {"insufficiente": True, "spiegazione": {}, "verdetti": []}
        else:
            client.reply = REPLY
        return await FakeClient.create(client, **kwargs)

    monkeypatch.setattr(explanations, "openai_client", lambda: client)
    monkeypatch.setattr(client, "create", create)
    monkeypatch.setattr(explanations, "corpus_and_index", lambda: ({"reg": {}, "cds": {}}, {}))
    monkeypatch.setattr(explanations, "select_articles", lambda *a, **k: ARTICLE)
    monkeypatch.setattr(explanations, "CONTENT_OUT", tmp_path)
    monkeypatch.setattr(explanations, "_locks", {})
    explanations._missing.clear()

    async with api_db() as s:
        outcome = await explanations.generate(s, 1)

    assert len(calls) == 2, "a decline was not retried"
    assert outcome.outcome == "stored", "the retry's result was thrown away"


async def test_it_does_not_retry_forever(api_db, monkeypatch, tmp_path):
    """A cluster whose articles genuinely do not cover the statements declines every time.
    Paying repeatedly to hear that is how a per-request cost becomes unbounded."""
    from tests.test_explanation_service import FakeClient

    client = FakeClient(reply={"insufficiente": True, "spiegazione": {}, "verdetti": []})
    monkeypatch.setattr(explanations, "openai_client", lambda: client)
    monkeypatch.setattr(explanations, "corpus_and_index", lambda: ({"reg": {}, "cds": {}}, {}))
    monkeypatch.setattr(explanations, "select_articles", lambda *a, **k: ARTICLE)
    monkeypatch.setattr(explanations, "CONTENT_OUT", tmp_path)
    monkeypatch.setattr(explanations, "_locks", {})
    explanations._missing.clear()

    async with api_db() as s:
        outcome = await explanations.generate(s, 1)

    assert client.calls == 2, f"asked {client.calls} times, expected exactly 2"
    assert outcome.outcome == "declined"


async def test_the_retry_reports_both_calls_tokens(api_db, monkeypatch, tmp_path):
    """The batch caller reports spend. Dropping the first attempt's tokens would understate
    what a retry actually costs, which is the number the decision to retry rests on."""
    from tests.test_explanation_service import FakeClient, REPLY

    calls = []
    client = FakeClient()

    async def create(**kwargs):
        calls.append(1)
        client.reply = ({"insufficiente": True, "spiegazione": {}, "verdetti": []}
                        if len(calls) == 1 else REPLY)
        return await FakeClient.create(client, **kwargs)

    monkeypatch.setattr(explanations, "openai_client", lambda: client)
    monkeypatch.setattr(client, "create", create)
    monkeypatch.setattr(explanations, "corpus_and_index", lambda: ({"reg": {}, "cds": {}}, {}))
    monkeypatch.setattr(explanations, "select_articles", lambda *a, **k: ARTICLE)
    monkeypatch.setattr(explanations, "CONTENT_OUT", tmp_path)
    monkeypatch.setattr(explanations, "_locks", {})
    explanations._missing.clear()

    async with api_db() as s:
        outcome = await explanations.generate(s, 1)
    assert outcome.tokens_in == 200, f"reported {outcome.tokens_in}, both calls were 100 each"
