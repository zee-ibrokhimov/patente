"""On-demand generation: the cache, the lock, and who pays for a call.

None of this existed while explanations were produced offline. What it defends:

  · a second request never pays for a second call
  · ten simultaneous requests for the same cluster make ONE call, and the loser of the
    race does not overwrite the winner
  · a user without entitlement never triggers a call at all
  · one call stores all three languages, and an approved wording is never replaced

No real API call is made — `openai_client` is substituted.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from api.models import Explanation, Question, User
from api.services import explanations
from api.services.entitlement import Access, Entitlement
from shared.config import settings
from shared.constants import STATUS_APPROVED, STATUS_DRAFT, STATUS_FLAGGED

REPLY = {
    "insufficiente": False,
    "spiegazione": {
        "it": "Il segnale DARE PRECEDENZA impone di cedere il passo (art. 106 Reg.).",
        "ru": "Знак УСТУПИТЕ ДОРОГУ обязывает уступить (art. 106 Reg.).",
        "en": "The GIVE WAY sign requires yielding (art. 106 Reg.).",
    },
    "articolo_citato": "art. 106 Reg.",
    "segnale_riconosciuto": "DARE PRECEDENZA",
    "verdetti": [{"n": 1, "risposta": "VERO", "certezza": "alta"},
                 {"n": 2, "risposta": "FALSO", "certezza": "alta"}],
}


class FakeClient:
    """Counts calls and hands back a canned reply after an await point.

    The await matters: without one, the concurrency test would pass even with no lock,
    because nothing would ever yield control between the cache check and the write.
    """

    def __init__(self, reply=None, delay=0.01, error=None):
        self.reply = reply if reply is not None else REPLY
        self.delay = delay
        self.error = error
        self.calls = 0
        self.messages = None
        self.chat = self                 # client.chat.completions.create
        self.completions = self

    async def create(self, **kwargs):
        self.calls += 1
        self.messages = kwargs.get("messages")
        await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        payload = json.dumps(self.reply, ensure_ascii=False)
        return type("R", (), {
            "choices": [type("C", (), {"message": type("M", (), {"content": payload})()})()],
            "usage": type("U", (), {"prompt_tokens": 100, "completion_tokens": 20})(),
        })()


@pytest.fixture
def fake_openai(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(explanations, "openai_client", lambda: client)
    monkeypatch.setattr(explanations, "_locks", {})
    # Real statute would mean loading 1.5M characters of JSON per test.
    monkeypatch.setattr(
        explanations, "corpus_and_index",
        lambda: ({"reg": {"106": {"number": "106", "rubric": "Segnale di dare precedenza",
                                  "text": "1. Il segnale DARE PRECEDENZA deve essere usato…",
                                  "figures": [], "sign_names": [], "signs": {}}},
                  "cds": {}}, {}),
    )
    monkeypatch.setattr(
        explanations, "select_articles",
        lambda *a, **k: [{"source": "reg", "number": "106",
                          "rubric": "Segnale di dare precedenza",
                          "text": "1. Il segnale DARE PRECEDENZA deve essere usato…"}],
    )
    return client


@pytest.fixture
async def cluster(api_db, tmp_path, monkeypatch):
    """`api_db`'s cluster 1 with its explanations cleared, so it is a cache miss.

    Cluster 1 is the right one to borrow: two statements, one VERO and one FALSO, and a
    figure — which is what the answer-key gate and the vision path both need. Its topic
    is mapped in TOPIC_ARTICLES, though `select_articles` is stubbed anyway.
    """
    async with api_db() as s:
        for row in (await s.scalars(select(Explanation))).all():
            await s.delete(row)
        await s.commit()

    # figure_part reads from CONTENT_OUT, so the referenced file has to exist somewhere.
    images = tmp_path / "images"
    images.mkdir(parents=True, exist_ok=True)
    (images / "sign_a.jpeg").write_bytes(b"\xff\xd8\xff\xe0 not really a jpeg \xff\xd9")
    monkeypatch.setattr(explanations, "CONTENT_OUT", tmp_path)
    return 1


# --- the cache ---------------------------------------------------------------

async def test_first_request_generates_and_stores_every_language(api_db, fake_openai, cluster):
    async with api_db() as s:
        row = await explanations.ensure(s, cluster, "ru")
        assert row is not None
        assert row.lang == "ru"
        assert row.status == STATUS_DRAFT

        stored = {e.lang: e.text for e in (await s.scalars(
            select(Explanation).where(Explanation.cluster_id == cluster)
        )).all()}
    assert set(stored) == {"it", "ru", "en"}
    assert stored["en"].startswith("The GIVE WAY sign")
    assert fake_openai.calls == 1


async def test_a_second_request_costs_nothing(api_db, fake_openai, cluster):
    async with api_db() as s:
        await explanations.ensure(s, cluster, "ru")
        await explanations.ensure(s, cluster, "ru")
        # A different language is a cache hit too — one call produced all three.
        await explanations.ensure(s, cluster, "en")
    assert fake_openai.calls == 1


async def test_simultaneous_requests_make_one_call(api_db, fake_openai, cluster):
    """Ten users answering the same question at once. Without the per-cluster lock this
    is ten paid calls, and the last writer wins over the others."""
    async with api_db() as s:
        await asyncio.gather(*(explanations.ensure(s, cluster, "ru") for _ in range(10)))
        rows = (await s.scalars(
            select(Explanation).where(Explanation.cluster_id == cluster)
        )).all()
    assert fake_openai.calls == 1
    assert len(rows) == 3          # it, ru, en — not thirty


async def test_the_figure_is_attached_to_the_request(api_db, fake_openai, cluster):
    """The whole reason vision was added: the statements say "il segnale raffigurato"."""
    async with api_db() as s:
        await explanations.ensure(s, cluster, "it")
    content = fake_openai.messages[-1]["content"]
    kinds = [part["type"] for part in content]
    assert "image_url" in kinds
    url = next(p["image_url"]["url"] for p in content if p["type"] == "image_url")
    assert url.startswith("data:image/jpeg;base64,")


# --- what is stored ----------------------------------------------------------

async def test_a_disagreement_with_the_answer_key_flags_every_language(
    api_db, fake_openai, cluster
):
    """Statement 2 is stored FALSO; the model is made to say VERO."""
    fake_openai.reply = {**REPLY, "verdetti": [
        {"n": 1, "risposta": "VERO", "certezza": "alta"},
        {"n": 2, "risposta": "VERO", "certezza": "alta"},
    ]}
    async with api_db() as s:
        await explanations.ensure(s, cluster, "it")
        rows = (await s.scalars(
            select(Explanation).where(Explanation.cluster_id == cluster)
        )).all()
    assert {r.status for r in rows} == {STATUS_FLAGGED}
    assert all("argues against the stored answer" in r.flags for r in rows)
    assert all("q2: key FALSO, model VERO" in r.flags for r in rows)


async def test_an_approved_wording_is_never_replaced(api_db, fake_openai, cluster):
    async with api_db() as s:
        s.add(Explanation(cluster_id=cluster, lang="ru", text="Проверено человеком.",
                          status=STATUS_APPROVED))
        await s.commit()
        await explanations.generate(s, cluster)
        row = await s.scalar(
            select(Explanation).where(Explanation.cluster_id == cluster,
                                      Explanation.lang == "ru")
        )
        assert row.text == "Проверено человеком."
        assert row.status == STATUS_APPROVED
        # The languages nobody approved were still written.
        italian = await s.scalar(
            select(Explanation).where(Explanation.cluster_id == cluster,
                                      Explanation.lang == "it")
        )
        assert italian.status == STATUS_DRAFT


async def test_a_decline_writes_nothing_so_the_next_request_retries(
    api_db, fake_openai, cluster
):
    """Declines are roughly a third of calls and partly run-to-run noise, so nothing is
    stored and asking again is the recovery."""
    fake_openai.reply = {"insufficiente": True, "spiegazione": {}, "verdetti": []}
    async with api_db() as s:
        result = await explanations.generate(s, cluster)
        assert result.outcome == "declined"
        assert (await s.scalars(select(Explanation))).all() == []


async def test_an_api_failure_is_swallowed_and_reported(api_db, fake_openai, cluster):
    """A user gets "not available", never a stack trace, and nothing is cached."""
    fake_openai.error = RuntimeError("Error code: 401 - invalid_api_key")
    async with api_db() as s:
        result = await explanations.generate(s, cluster)
    assert result.outcome == "error"
    assert result.fatal is True


# --- who pays ----------------------------------------------------------------

async def test_an_unentitled_user_never_triggers_a_call(api_db, fake_openai, cluster):
    """Entitlement is checked before the API call, which is the point of checking."""
    broke = Entitlement(has_pass=False, pass_expires_at=None, free_explanations_left=0)

    async with api_db() as s:
        question = await s.get(Question, 1)
        user = type("U", (), {"chat_id": 42, "lang": "ru"})()
        payload, access = await explanations.deliver(s, question, user, broke)

    assert access is Access.LOCKED
    assert payload["explanation"] is None
    assert fake_openai.calls == 0


async def test_a_withheld_draft_does_not_cost_the_user_a_taster(
    api_db, fake_openai, cluster
):
    fake_openai.reply = {**REPLY, "verdetti": [
        {"n": 1, "risposta": "VERO", "certezza": "alta"},
        {"n": 2, "risposta": "VERO", "certezza": "alta"},   # disagrees -> flagged
    ]}
    entitled = Entitlement(has_pass=False, pass_expires_at=None, free_explanations_left=3)

    async with api_db() as s:
        question = await s.get(Question, 1)
        user = type("U", (), {"chat_id": 42, "lang": "ru", "free_explanations_used": 0})()
        payload, access = await explanations.deliver(s, question, user, entitled)

    assert access is Access.UNAVAILABLE
    assert payload["explanation"] is None
    assert user.free_explanations_used == 0


# --- warming at question-serve time ------------------------------------------

async def test_serving_a_question_warms_its_explanation(
    client, api_db, fake_openai, cluster, monkeypatch
):
    """The trigger the product wants: by the time the user answers, it is already there.

    Warming runs after the response is sent, so nothing about the question is delayed —
    but the explanation is cached before the answer arrives.
    """
    monkeypatch.setattr(explanations, "async_session_factory", lambda: api_db)
    await client.post("/users", json={"chat_id": 42, "lang": "ru"})
    async with api_db() as s:
        user = await s.get(User, 42)
        user.pass_expires_at = datetime.now(timezone.utc) + timedelta(days=30)
        await s.commit()

    served = await client.get("/users/42/next-question?topic_id=1&exclude_id=2")
    assert served.status_code == 200
    assert fake_openai.calls == 1

    answered = (await client.post(
        "/users/42/answers", json={"question_id": 1, "answer": True}
    )).json()
    assert answered["explanation_state"] == "shown"
    assert answered["explanation"].startswith("Знак")
    # Still one call: answering served the cache and never generated.
    assert fake_openai.calls == 1


async def test_a_free_user_with_no_taster_is_not_warmed_for(
    client, api_db, fake_openai, cluster, monkeypatch
):
    """Generating for someone who cannot be shown the result is money spent on nothing."""
    monkeypatch.setattr(explanations, "async_session_factory", lambda: api_db)
    monkeypatch.setattr(settings, "free_explanations", 0)
    await client.post("/users", json={"chat_id": 42, "lang": "ru"})

    await client.get("/users/42/next-question?topic_id=1&exclude_id=2")
    assert fake_openai.calls == 0


# --- parsing the reply -------------------------------------------------------

def test_a_bare_string_reply_is_accepted_as_italian():
    """A model that answers with one explanation instead of three has still answered."""
    assert explanations.parsed_texts({"spiegazione": "Una regola."}) == {"it": "Una regola."}


def test_languages_we_do_not_serve_are_dropped():
    texts = explanations.parsed_texts(
        {"spiegazione": {"it": "a", "ru": "b", "en": "c", "de": "d", "fr": ""}}
    )
    assert set(texts) == {"it", "ru", "en"}


def test_a_missing_italian_is_treated_as_no_answer():
    """Italian is the canonical text: the gates read it and the reviewer reads it."""
    assert "it" not in explanations.parsed_texts({"spiegazione": {"en": "only english"}})
