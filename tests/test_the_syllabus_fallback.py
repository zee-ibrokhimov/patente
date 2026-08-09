"""Questions the Codice does not answer still get an explanation — and never a fake citation.

Part of the ministerial syllabus is not statute. Cluster 1487 asks what failing to obey the
rules for merging leads to; others ask how to treat a casualty. Handed only the Codice the
model declines, and it is RIGHT to — the answer is not in what it was given, and no amount
of retrieval reaches text that was never written.

So the third attempt changes the question rather than the search: explain from the exam
syllabus, cite nothing. Measured — 1487 declined twice and then produced a sound explanation
flagged "explained from the exam syllabus".

The danger this file exists to guard is the obvious one. A model told "you may answer from
general knowledge" will happily produce "(art. 154 C.d.S.)" to look authoritative, and a
wrong article number is worse than none: it is checkable, and a candidate who checks it
stops trusting everything else on the screen.
"""

from __future__ import annotations

from api.services.explanations import (
    SYLLABUS_PROMPT,
    SYSTEM_PROMPT,
    build_messages,
    check_gates,
)
from shared.constants import STATUS_DRAFT, STATUS_FLAGGED


def system_of(messages) -> str:
    return next(m["content"] for m in messages if m["role"] == "system")


ARGS = ("Norme sul sorpasso", [{"statement": "x", "id": 1, "answer": True}], [], None)


# --- which prompt is used ---------------------------------------------------

def test_the_normal_path_is_unchanged():
    assert system_of(build_messages(*ARGS)) == SYSTEM_PROMPT


def test_the_fallback_swaps_the_prompt():
    assert system_of(build_messages(*ARGS, syllabus=True)) == SYLLABUS_PROMPT


def test_the_two_prompts_actually_differ():
    """A replace() that silently matches nothing would leave the fallback identical to the
    strict prompt, and the third attempt would just be a third refusal."""
    assert SYLLABUS_PROMPT != SYSTEM_PROMPT, "SYLLABUS_PROMPT did not substitute anything"


# --- what the fallback forbids ----------------------------------------------

def test_the_fallback_forbids_inventing_a_citation():
    assert "NON citare alcun articolo" in SYLLABUS_PROMPT
    assert '"articolo_citato" vuoto' in SYLLABUS_PROMPT


def test_the_fallback_drops_the_statute_requirement():
    """The strict prompt's instruction to answer "insufficiente" when the articles do not
    decide is the thing being removed. If it survives, the model declines again."""
    strict = 'Se gli articoli forniti non bastano a decidere, imposta "insufficiente": true'
    assert strict in SYSTEM_PROMPT
    assert strict not in SYLLABUS_PROMPT


def test_the_fallback_still_allows_an_honest_refusal():
    """Permission to answer from the syllabus is not an order to answer. A model that does
    not know must still be able to say so, or the fallback buys invention."""
    assert '"insufficiente": true' in SYLLABUS_PROMPT


# --- the numeric gate under the fallback ------------------------------------

def parsed(text: str) -> dict:
    return {
        "insufficiente": False,
        "spiegazione": {"it": text, "ru": text, "en": text, "uz": text},
        "verdetti": [{"n": 1, "risposta": "VERO", "certezza": "alta"}],
    }


JUDGED = [{"id": 1, "answer": True, "statement": "x"}]


def test_an_unverifiable_number_is_withheld():
    """THE risk. With no article behind it there is nothing to check a figure against, and
    a wrong speed or distance is the worst sentence this product can print. Syllabus mode
    passes no articles, so any number is ungrounded and the cluster is withheld."""
    status, reasons, _ = check_gates(parsed("Mantieni almeno 50 metri di distanza."), JUDGED, [])
    assert status == STATUS_FLAGGED
    assert any("number" in r for r in reasons), reasons


def test_prose_without_numbers_passes():
    """First-aid and behaviour answers normally carry no figures, which is why the strict
    numeric rule costs little — it must not withhold them."""
    status, _, _ = check_gates(
        parsed("Occorre fermarsi e prestare assistenza senza spostare il ferito."), JUDGED, [])
    assert status == STATUS_DRAFT


# --- the wiring, not just the constants -------------------------------------
#
# Everything above tests the PROMPTS. Two mutations proved that insufficient: setting the
# retry bound so the fallback never fires, and grounding syllabus numbers against the
# articles again, both left every test above green. These exercise generate() itself.

from api.services import explanations

ARTICLE = [{"source": "cds", "number": "148", "rubric": "Sorpasso", "text": "testo"}]
REFUSAL = {"insufficiente": True, "spiegazione": {}, "verdetti": []}


def _stub(monkeypatch, tmp_path, client):
    monkeypatch.setattr(explanations, "openai_client", lambda: client)
    monkeypatch.setattr(explanations, "corpus_and_index", lambda: ({"reg": {}, "cds": {}}, {}))
    monkeypatch.setattr(explanations, "select_articles", lambda *a, **k: ARTICLE)
    monkeypatch.setattr(explanations, "CONTENT_OUT", tmp_path)
    monkeypatch.setattr(explanations, "_locks", {})
    explanations._missing.clear()


async def test_generate_reaches_the_syllabus_attempt(api_db, monkeypatch, tmp_path):
    """The fallback must actually be REACHED. A retry bound of `attempt < 1` leaves every
    prompt assertion in this file passing while the third attempt never happens."""
    from tests.test_explanation_service import FakeClient

    seen: list[str] = []

    class Recording(FakeClient):
        async def create(self, **kwargs):
            seen.append(next(m["content"] for m in kwargs["messages"] if m["role"] == "system"))
            return await super().create(**kwargs)

    _stub(monkeypatch, tmp_path, Recording(reply=REFUSAL))
    async with api_db() as s:
        await explanations.generate(s, 1)

    assert len(seen) == 3, f"{len(seen)} attempts; the syllabus attempt was not reached"
    assert seen[:2] == [explanations.SYSTEM_PROMPT] * 2
    assert seen[2] == explanations.SYLLABUS_PROMPT


async def test_a_syllabus_answer_with_a_number_is_withheld(api_db, monkeypatch, tmp_path):
    """End to end, not via check_gates directly. Grounding syllabus numbers against the
    articles again is a one-word change that no prompt assertion notices."""
    from tests.test_explanation_service import FakeClient

    answer = "Serve una distanza di almeno 50 metri."

    # The stub article below CONTAINS "50 metri" on purpose, and is applied AFTER _stub so
    # it wins. With a placeholder body the figure is ungrounded whichever list is passed, so
    # the test passed under the very mutation it exists to catch. Now the paths disagree:
    # grounded against this article 50 is legitimate; grounded against nothing it is not.
    class Late(FakeClient):
        async def create(self, **kwargs):
            system = next(m["content"] for m in kwargs["messages"] if m["role"] == "system")
            if system == explanations.SYLLABUS_PROMPT:
                self.reply = {
                    "insufficiente": False,
                    "spiegazione": {"it": answer, "ru": answer, "en": answer, "uz": answer},
                    "verdetti": [{"n": 1, "risposta": "VERO", "certezza": "alta"}],
                }
            return await super().create(**kwargs)

    _stub(monkeypatch, tmp_path, Late(reply=REFUSAL))
    monkeypatch.setattr(explanations, "select_articles", lambda *a, **k: [
        {"source": "cds", "number": "149", "rubric": "Distanza di sicurezza",
         "text": "La distanza di sicurezza non puo essere inferiore a 50 metri."},
    ])
    async with api_db() as s:
        outcome = await explanations.generate(s, 1)
        assert outcome.outcome == "stored"
        row = outcome.row or await explanations.existing(s, 1, "it")

    assert row.status == "flagged", (
        "a figure invented without any article behind it was served to a learner")
    assert "number" in (row.flags or ""), row.flags
