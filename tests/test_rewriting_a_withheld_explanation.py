"""Rewriting the explanations the quality gates refused to serve.

Measured on the live bank: 15 clusters are withheld, covering 119 questions, and the
learner sees "not available yet" with no button — the one state in this product that reads
as a fault rather than as a choice. The reasons split four ways:

    7  argued against the ministry's answer
    6  cited a number the cited article does not contain
    3  the model reported low confidence
    3  no article covers it, so it was written from the syllabus

ONLY THE FIRST IS FIXED BY ASKING AGAIN, and not by asking the same question. The model has
not changed its mind, so a plain retry reproduces the argument and the same withheld text.
Handing it the official answers changes the task from "judge this" to "explain why the
examiner's answer is what it is", which is the thing a learner sitting the exam needs.

THE DANGER THIS FILE EXISTS TO GUARD is that the change becomes a bypass. Supplying the key
makes the answer-key gate vacuous — of course the model now agrees — so:

  · every OTHER gate must still run, and an invented speed limit must still be withheld;
  · the row must say the verdicts were not independent, or a reviewer reading it later will
    take the agreement as corroboration when it is nothing of the sort.
"""

from __future__ import annotations

from sqlalchemy import select as sa_select

from api.services import explanations
from shared.constants import LANG_IT, STATUS_DRAFT, STATUS_FLAGGED


# --- the prompt ---------------------------------------------------------------

MEMBERS = [
    {"id": 1, "statement": "I tricicli a motore hanno tre ruote simmetriche", "answer": True},
    {"id": 2, "statement": "I tricicli a motore possono avere un sidecar", "answer": False},
]
ARTICLE = [{"source": "cds", "number": "47", "rubric": "Classificazione",
            "text": "I veicoli si classificano come segue."}]


def test_the_ordinary_prompt_does_not_reveal_the_answers():
    """THE default, and the reason the answer-key gate can work at all. The model judges
    each statement independently; a disagreement is evidence, and it is how a wrong
    explanation is caught before a learner reads it."""
    prompt = explanations.build_text_prompt("Definizioni", MEMBERS, ARTICLE)
    assert "VERO" not in prompt and "FALSO" not in prompt
    assert "UFFICIALI" not in prompt


def test_the_rewrite_prompt_supplies_them_in_order():
    prompt = explanations.build_text_prompt("Definizioni", MEMBERS, ARTICLE, with_key=True)
    assert "RISPOSTE UFFICIALI" in prompt
    body = prompt[prompt.index("RISPOSTE UFFICIALI"):]
    assert "1. VERO" in body and "2. FALSO" in body, body


def test_the_rewrite_prompt_forbids_arguing_with_the_key():
    """Without this the model answers the question it was originally asked and disputes the
    answer again, which is the exact outcome being rewritten away from."""
    prompt = explanations.build_text_prompt("Definizioni", MEMBERS, ARTICLE, with_key=True)
    lowered = prompt.lower()
    assert "non contestarle" in lowered
    assert "perché" in lowered, "it must ask WHY the official answer is what it is"


def test_the_answers_reach_the_model_only_when_asked():
    """`build_messages` is what the generator actually calls; a flag that stopped short of
    it would leave the whole feature inert while every prompt test above still passed."""
    plain = explanations.build_messages("Definizioni", MEMBERS, ARTICLE, None)
    keyed = explanations.build_messages("Definizioni", MEMBERS, ARTICLE, None, with_key=True)
    assert "RISPOSTE UFFICIALI" not in str(plain)
    assert "RISPOSTE UFFICIALI" in str(keyed)


# --- the gates still run --------------------------------------------------------

def _parsed(verdicts, italian="Il triciclo ha tre ruote simmetriche (art. 47 C.d.S.)."):
    return {
        "verdetti": [{"n": i, "risposta": v, "certezza": "alta"}
                     for i, v in enumerate(verdicts, 1)],
        "spiegazione": {"it": italian, "ru": "…", "en": "…", "uz": "…"},
    }


def test_an_invented_number_is_still_withheld_after_a_rewrite():
    """The worst thing this product can say is a wrong speed limit, and supplying the answer
    key has nothing to do with whether a number is real. This gate must be untouched."""
    status, reasons, _ = explanations.check_gates(
        _parsed(["VERO", "FALSO"], "Il limite è 70 km/h (art. 47 C.d.S.)."),
        MEMBERS, ARTICLE)
    assert status == STATUS_FLAGGED
    assert any("number" in r for r in reasons), reasons


def test_low_confidence_is_still_withheld():
    parsed = _parsed(["VERO", "FALSO"])
    parsed["verdetti"][0]["certezza"] = "bassa"
    status, reasons, _ = explanations.check_gates(parsed, MEMBERS, ARTICLE)
    assert status == STATUS_FLAGGED
    assert any("confidence" in r for r in reasons)


def test_agreeing_with_the_key_is_a_clean_row():
    """The outcome being aimed at: the model explains the official answer, nothing else
    trips, and the learner finally sees an explanation."""
    status, reasons, disagreements = explanations.check_gates(
        _parsed(["VERO", "FALSO"]), MEMBERS, ARTICLE)
    assert status == STATUS_DRAFT
    assert not reasons and not disagreements


# --- choosing what to rewrite ----------------------------------------------------

async def _cluster(api_db, cluster_id: int, status: str, questions: int = 1):
    from api.models import Cluster, Explanation, Question, Quesito

    async with api_db() as s:
        s.add(Cluster(id=cluster_id, natural_key=f"k{cluster_id}", rule_summary="r"))
        s.add(Quesito(id=9000 + cluster_id, topic_id=1, primary_image=None))
        await s.flush()
        for i in range(questions):
            s.add(Question(id=cluster_id * 100 + i, quesito_id=9000 + cluster_id, topic_id=1,
                           cluster_id=cluster_id, statement_it=f"s{cluster_id}-{i}",
                           answer=True, source_version="v1"))
        if status:
            s.add(Explanation(cluster_id=cluster_id, lang=LANG_IT, text="t", status=status))
        await s.commit()


async def test_only_withheld_clusters_are_chosen(api_db):
    """Not "every flagged row": a cluster with a flagged Uzbek row AND a servable Italian one
    is already answering the learner, and rewriting it spends money to change nothing."""
    await _cluster(api_db, 501, STATUS_FLAGGED)
    await _cluster(api_db, 502, STATUS_DRAFT)
    await _cluster(api_db, 503, "")

    async with api_db() as s:
        picked = await explanations.withheld_clusters(s)
    assert picked == [501], picked


async def test_a_cluster_with_both_a_flagged_and_a_servable_row_is_left_alone(api_db):
    from api.models import Explanation

    await _cluster(api_db, 504, STATUS_FLAGGED)
    async with api_db() as s:
        s.add(Explanation(cluster_id=504, lang="ru", text="t", status=STATUS_DRAFT))
        await s.commit()
        assert await explanations.withheld_clusters(s) == []


async def test_the_biggest_hole_is_rewritten_first(api_db):
    """A withheld cluster is a hole exactly as wide as the number of questions in it."""
    await _cluster(api_db, 505, STATUS_FLAGGED, questions=1)
    await _cluster(api_db, 506, STATUS_FLAGGED, questions=5)
    await _cluster(api_db, 507, STATUS_FLAGGED, questions=3)

    async with api_db() as s:
        assert await explanations.withheld_clusters(s) == [506, 507, 505]


# --- the row says where it came from ---------------------------------------------

def test_a_supplied_key_is_recorded_on_the_row():
    """THE honesty guard.

    Supplying the key makes the answer-key gate vacuous — of course the model agrees now.
    A reviewer reading the row later must not take that agreement as corroboration, so the
    row says where it came from. Without it, "model agrees with the key" means two
    completely different things depending on a fact recorded nowhere.

    Tested on `provenance` directly. The test below drives the whole rewrite with a stubbed
    generator, and a stub that hardcodes the note proves only that the stub can type — a
    mutant deleting the real note survived exactly that test.
    """
    assert explanations.SUPPLIED_KEY_NOTE in explanations.provenance([], with_key=True)
    assert explanations.SUPPLIED_KEY_NOTE not in explanations.provenance([])


def test_the_syllabus_note_is_recorded_too():
    """That text cites no article BY DESIGN. A reviewer comparing it against the Codice
    would otherwise conclude the citation had gone missing."""
    assert explanations.SYLLABUS_NOTE in explanations.provenance([], attempt=2)
    assert explanations.SYLLABUS_NOTE not in explanations.provenance([], attempt=1)


def test_provenance_keeps_the_real_reasons():
    out = explanations.provenance(["contains a number"], attempt=2, with_key=True)
    assert out[0] == "contains a number"
    assert len(out) == 3


def test_a_provenance_note_never_withholds_a_row():
    """The status is decided by `check_gates` BEFORE the notes are added. If a note could
    flag a row, every syllabus-written explanation in the bank would be withheld — which is
    three clusters today and would be far more once the bulk generator runs."""
    import inspect

    body = inspect.getsource(explanations.generate)
    at_gate = body.index("check_gates(")
    at_note = body.index("provenance(")
    assert at_gate < at_note, "the notes are added before the status is decided"
    assert "status" not in body[at_note:body.index("record_flags", at_note)], \
        "the status is recomputed after a note is appended"


# --- the rewrite records what it is ----------------------------------------------

async def test_a_rewrite_says_its_verdicts_are_not_independent(api_db, monkeypatch):
    """THE honesty guard.

    Supplying the key makes the answer-key gate vacuous — of course the model agrees now.
    A reviewer reading the row later must not take that agreement as corroboration, so the
    row says where it came from. Without this, "model agrees with the key" would mean two
    completely different things depending on a fact nowhere in the database.
    """
    from api.models import Explanation

    await _cluster(api_db, 508, STATUS_FLAGGED)

    seen: dict = {}

    async def fake_generate(session, cluster_id, model=None, attempt=0, with_key=False):
        # REPLACES the flagged row rather than adding beside it, which is what the real
        # generator does — (cluster_id, lang) is unique, and a fake that inserts would pass
        # a test the production path could never satisfy.
        seen["with_key"] = with_key
        row = await explanations.existing(session, cluster_id, LANG_IT)
        row.text, row.status = "rewritten", STATUS_DRAFT
        row.flags = ("rewritten with the official answers supplied — the verdicts "
                     "are not independent")
        await session.flush()
        return explanations.Outcome("stored")

    monkeypatch.setattr(explanations, "generate", fake_generate)
    async with api_db() as s:
        result = await explanations.rewrite_withheld(s, limit=10)

    assert seen["with_key"] is True, "the rewrite asked the same question as before"
    assert result["served"] == 1
    async with api_db() as s:
        # `select(Explanation)`, not `Explanation.__table__.select()` — the second yields
        # ROWS, so scalars() hands back the first column (the id) and every attribute probe
        # below fails on an int.
        rows = list(await s.scalars(
            sa_select(Explanation).where(Explanation.cluster_id == 508)))
    assert any("not independent" in (r.flags or "") for r in rows), \
        "nothing on the row says the verdicts were supplied"


async def test_a_rewrite_that_is_still_withheld_is_counted_not_hidden(api_db, monkeypatch):
    """The six clusters flagged for inventing a number will very likely still be flagged.
    The count has to say so, or the owner runs it, sees "done", and believes the holes are
    closed."""
    await _cluster(api_db, 509, STATUS_FLAGGED)

    async def still_bad(session, cluster_id, model=None, attempt=0, with_key=False):
        row = await explanations.existing(session, cluster_id, LANG_IT)
        row.text, row.status, row.flags = "x", STATUS_FLAGGED, "contains a number…"
        await session.flush()
        return explanations.Outcome("stored")

    monkeypatch.setattr(explanations, "generate", still_bad)
    async with api_db() as s:
        result = await explanations.rewrite_withheld(s, limit=10)
    assert result == {"clusters": 1, "served": 0, "still_withheld": 1, "failed": 0}


async def test_a_failed_call_does_not_count_as_served(api_db, monkeypatch):
    await _cluster(api_db, 510, STATUS_FLAGGED)

    async def broken(session, cluster_id, model=None, attempt=0, with_key=False):
        return explanations.Outcome("error", detail="boom")

    monkeypatch.setattr(explanations, "generate", broken)
    async with api_db() as s:
        result = await explanations.rewrite_withheld(s, limit=10)
    assert result["failed"] == 1 and result["served"] == 0


# --- the endpoint -----------------------------------------------------------------

async def test_the_endpoint_is_staff_only(client, registered):
    import json
    import time

    from api.services.telegram_auth import sign
    from shared.config import settings

    token = "8918020834:AAEtest-token-not-real-only-for-tests"
    settings.bot_token_prod = token
    settings.env = "prod"
    headers = {"X-Telegram-Init-Data": sign(
        {"user": json.dumps({"id": 42}, separators=(",", ":")),
         "auth_date": str(int(time.time()))}, token)}

    r = await client.post("/webapp/admin/content/rewrite-withheld", headers=headers)
    assert r.status_code == 404, "a learner could rewrite the content bank"


async def test_each_cluster_is_committed_as_it_is_written(api_db, monkeypatch):
    """FOUND BY RUNNING IT AGAINST PRODUCTION, not by a test.

    The first version committed once at the end, so a single transaction stayed open across
    sixteen model calls — half a minute of network with the session dirty. Every SELECT in
    the loop then had to upgrade that transaction to a write, which SQLite refuses outright
    while anything else holds the lock, and the run died with "database is locked" after 36
    seconds having written nothing.

    Asserted by counting COMMITS against clusters. Watching from a second session cannot
    tell flush from commit here — the test harness shares one connection — and a test that
    cannot tell them apart is exactly the one that let this ship.
    """
    await _cluster(api_db, 511, STATUS_FLAGGED, questions=2)
    await _cluster(api_db, 512, STATUS_FLAGGED, questions=1)

    order: list[str] = []

    async def rewrite_one(session, cluster_id, model=None, attempt=0, with_key=False):
        order.append(f"generate:{cluster_id}")
        row = await explanations.existing(session, cluster_id, LANG_IT)
        row.text, row.status, row.flags = "ok", STATUS_DRAFT, explanations.SUPPLIED_KEY_NOTE
        await session.flush()
        return explanations.Outcome("stored")

    monkeypatch.setattr(explanations, "generate", rewrite_one)
    async with api_db() as s:
        real_commit = s.commit

        async def watched():
            order.append("commit")
            await real_commit()

        s.commit = watched
        await explanations.rewrite_withheld(s, limit=10)

    assert order == ["generate:511", "commit", "generate:512", "commit"], (
        f"work was not committed as it went: {order} — one transaction held across every "
        "model call is what caused the production failure"
    )


async def test_a_failed_cluster_does_not_poison_the_rest(api_db, monkeypatch):
    """A generation that raised left the session dirty, so the NEXT cluster's commit carried
    a half-written row with it. Rolling back keeps each cluster independent."""
    from api.models import Explanation

    await _cluster(api_db, 513, STATUS_FLAGGED, questions=5)
    await _cluster(api_db, 514, STATUS_FLAGGED, questions=1)

    async def first_fails(session, cluster_id, model=None, attempt=0, with_key=False):
        if cluster_id == 513:
            row = await explanations.existing(session, cluster_id, LANG_IT)
            row.text = "half written"
            await session.flush()
            return explanations.Outcome("error", detail="boom")
        row = await explanations.existing(session, cluster_id, LANG_IT)
        row.text, row.status = "ok", STATUS_DRAFT
        await session.flush()
        return explanations.Outcome("stored")

    monkeypatch.setattr(explanations, "generate", first_fails)
    async with api_db() as s:
        result = await explanations.rewrite_withheld(s, limit=10)

    assert result["failed"] == 1 and result["served"] == 1
    async with api_db() as s:
        failed_row = await explanations.existing(s, 513, LANG_IT)
    assert failed_row.text != "half written", \
        "a failed cluster's partial write was committed by the next cluster"
