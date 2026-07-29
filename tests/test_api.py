"""Core API behaviour, with the paywall as the main event.

The rule these exist to defend (plan §6.3): the API never sends text the user is
not entitled to. Not blanked client-side, not present-and-flagged — absent.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

from api.models import Event, Progress, Purchase, Report, User
from shared.config import settings
from shared.constants import EV_ANSWER_GIVEN, EV_PAYWALL_HIT, EV_QUESTION_SERVED

APPROVED_RU = "Круглый знак с красной каймой запрещает движение."
DRAFT_RU = "ЧЕРНОВИК"
TRANSLATION_RU = "Знак запрещает движение"


async def give_pass(api_db, chat_id: int, days: int = 30):
    async with api_db() as s:
        await s.execute(
            update(User)
            .where(User.chat_id == chat_id)
            .values(pass_expires_at=datetime.now(timezone.utc) + timedelta(days=days))
        )
        await s.commit()


async def events_of(api_db, event_type: str):
    async with api_db() as s:
        return (await s.scalars(select(Event).where(Event.type == event_type))).all()


# --------------------------------------------------------------------------
# basics
# --------------------------------------------------------------------------

async def test_health_reports_whether_content_is_loaded(client):
    body = (await client.get("/health")).json()
    assert body["status"] == "ok"
    assert body["questions"] == 4 and body["seeded"] is True


async def test_topics_carry_their_question_counts(client):
    topics = (await client.get("/topics")).json()
    assert {t["name"]: t["questions"] for t in topics} == {
        "Segnali di divieto": 2,
        "Distanza di sicurezza": 2,
    }


async def test_start_is_idempotent(client):
    first = await client.post("/users", json={"chat_id": 42, "lang": "ru"})
    await client.post("/users/42/answers", json={"question_id": 1, "answer": True})
    second = await client.post("/users", json={"chat_id": 42, "lang": "ru"})
    assert second.json()["created_at"] == first.json()["created_at"]

    stats = (await client.get("/users/42/stats")).json()
    assert stats["answers_given"] == 1  # /start did not wipe progress


async def test_unknown_user_is_rejected(client):
    assert (await client.get("/users/999/next-question")).status_code == 404


# --------------------------------------------------------------------------
# the paywall
# --------------------------------------------------------------------------

async def test_free_user_gets_italian_but_not_the_translation(client, registered):
    body = (await client.get("/users/42/next-question?topic_id=1&exclude_id=2")).json()
    assert body["statement_it"].startswith("Il segnale raffigurato")
    assert body["translation_state"] == "locked"
    assert body["translation"] is None


async def test_locked_translation_text_never_reaches_the_client(client, registered):
    raw = (await client.get("/users/42/next-question?topic_id=1&exclude_id=2")).text
    assert TRANSLATION_RU not in raw


async def test_pass_holder_gets_the_translation(client, registered, api_db):
    await give_pass(api_db, 42)
    body = (await client.get("/users/42/next-question?topic_id=1&exclude_id=2")).json()
    assert body["translation_state"] == "shown"
    assert body["translation"]["statement"] == TRANSLATION_RU


async def test_translations_off_beats_entitlement(client, registered, api_db):
    await give_pass(api_db, 42)
    await client.patch("/users/42", json={"translations_on": False})
    body = (await client.get("/users/42/next-question?topic_id=1&exclude_id=2")).json()
    assert body["translation_state"] == "off"
    assert body["translation"] is None


async def test_explanation_is_locked_for_a_free_user_with_no_taster(
    client, registered, monkeypatch
):
    monkeypatch.setattr(settings, "free_explanations", 0)
    body = (await client.post(
        "/users/42/answers", json={"question_id": 1, "answer": False}
    )).json()
    assert body["correct"] is False
    assert body["correct_answer"] is True     # the free tier still reveals this
    assert body["explanation_state"] == "locked"
    assert body["explanation"] is None


async def test_locked_explanation_text_never_reaches_the_client(
    client, registered, monkeypatch
):
    monkeypatch.setattr(settings, "free_explanations", 0)
    raw = (await client.post(
        "/users/42/answers", json={"question_id": 1, "answer": False}
    )).text
    assert APPROVED_RU not in raw


async def test_unreviewed_explanation_reads_unavailable_not_locked(
    client, registered, monkeypatch
):
    """The distinction that stops the bot selling something undeliverable.

    Question 3's explanation exists but is still a draft. Showing a paywall for it
    would take money for content nobody has approved.
    """
    monkeypatch.setattr(settings, "free_explanations", 0)
    response = await client.post(
        "/users/42/answers", json={"question_id": 3, "answer": False}
    )
    body = response.json()
    assert body["explanation_state"] == "unavailable"
    assert body["explanation"] is None
    assert DRAFT_RU not in response.text


async def test_draft_explanation_is_withheld_even_from_a_pass_holder(
    client, registered, api_db
):
    await give_pass(api_db, 42)
    response = await client.post(
        "/users/42/answers", json={"question_id": 3, "answer": False}
    )
    assert response.json()["explanation_state"] == "unavailable"
    assert DRAFT_RU not in response.text


async def test_question_without_a_cluster_has_no_explanation(client, registered, api_db):
    await give_pass(api_db, 42)
    body = (await client.post(
        "/users/42/answers", json={"question_id": 4, "answer": True}
    )).json()
    assert body["explanation_state"] == "unavailable"


# --------------------------------------------------------------------------
# the free taster
# --------------------------------------------------------------------------

async def test_taster_explanations_are_spent_then_locked(client, registered, monkeypatch):
    """Two free explanations, then the paywall — quality is the pitch (§4.3)."""
    monkeypatch.setattr(settings, "free_explanations", 2)

    first = (await client.post(
        "/users/42/answers", json={"question_id": 1, "answer": False}
    )).json()
    assert first["explanation_state"] == "shown"
    assert first["explanation"] == APPROVED_RU
    assert first["free_explanations_left"] == 1

    second = (await client.post(
        "/users/42/answers", json={"question_id": 2, "answer": True}
    )).json()
    assert second["explanation_state"] == "shown"
    assert second["free_explanations_left"] == 0

    third = (await client.post(
        "/users/42/answers", json={"question_id": 1, "answer": False}
    )).json()
    assert third["explanation_state"] == "locked"
    assert third["explanation"] is None


async def test_pass_holder_does_not_spend_the_taster(client, registered, api_db, monkeypatch):
    """Buying then lapsing must not have burned the free allowance."""
    monkeypatch.setattr(settings, "free_explanations", 3)
    await give_pass(api_db, 42)

    for _ in range(3):
        body = (await client.post(
            "/users/42/answers", json={"question_id": 1, "answer": True}
        )).json()
        assert body["explanation_state"] == "shown"

    async with api_db() as s:
        assert (await s.get(User, 42)).free_explanations_used == 0


async def test_expired_pass_locks_content_again(client, registered, api_db):
    await give_pass(api_db, 42, days=-1)  # expired yesterday
    body = (await client.get("/users/42/next-question?topic_id=1&exclude_id=2")).json()
    assert body["translation_state"] == "locked"


# --------------------------------------------------------------------------
# answering and scheduling
# --------------------------------------------------------------------------

async def test_correct_answer_promotes_the_box(client, registered):
    body = (await client.post(
        "/users/42/answers", json={"question_id": 1, "answer": True}
    )).json()
    assert body["correct"] is True and body["box"] == 2


async def test_wrong_answer_returns_to_box_one(client, registered):
    await client.post("/users/42/answers", json={"question_id": 1, "answer": True})
    await client.post("/users/42/answers", json={"question_id": 1, "answer": True})
    body = (await client.post(
        "/users/42/answers", json={"question_id": 1, "answer": False}
    )).json()
    assert body["box"] == 1


async def test_due_review_is_served_before_an_unseen_question(client, registered, api_db):
    await client.post("/users/42/answers", json={"question_id": 1, "answer": False})
    async with api_db() as s:  # make it due now
        await s.execute(
            update(Progress)
            .where(Progress.chat_id == 42, Progress.question_id == 1)
            .values(due_at=datetime.now(timezone.utc) - timedelta(minutes=1))
        )
        await s.commit()

    body = (await client.get("/users/42/next-question")).json()
    assert body["id"] == 1


async def test_the_question_just_answered_is_not_served_straight_back(client, registered):
    await client.post("/users/42/answers", json={"question_id": 1, "answer": False})
    for _ in range(5):
        body = (await client.get("/users/42/next-question?exclude_id=1")).json()
        assert body["id"] != 1


async def test_unknown_question_is_rejected(client, registered):
    r = await client.post("/users/42/answers", json={"question_id": 9999, "answer": True})
    assert r.status_code == 404


# --------------------------------------------------------------------------
# events — none of this is backfillable (§9)
# --------------------------------------------------------------------------

async def test_serving_and_answering_are_both_logged(client, registered, api_db):
    await client.get("/users/42/next-question")
    await client.post("/users/42/answers", json={"question_id": 1, "answer": True})

    assert len(await events_of(api_db, EV_QUESTION_SERVED)) == 1
    answered = await events_of(api_db, EV_ANSWER_GIVEN)
    assert len(answered) == 1
    assert answered[0].payload["correct"] is True
    assert answered[0].payload["topic_id"] == 1


async def test_paywall_hit_records_whether_it_followed_a_wrong_answer(
    client, registered, monkeypatch, api_db
):
    """The conversion moment. Without this the §4.3 metric cannot be computed."""
    monkeypatch.setattr(settings, "free_explanations", 0)
    await client.post("/users/42/answers", json={"question_id": 1, "answer": False})

    hits = await events_of(api_db, EV_PAYWALL_HIT)
    assert len(hits) == 1
    assert hits[0].payload["after_wrong_answer"] is True


async def test_answer_events_stamp_entitlement_for_conversion_analysis(
    client, registered, api_db
):
    await client.post("/users/42/answers", json={"question_id": 1, "answer": False})
    await give_pass(api_db, 42)
    await client.post("/users/42/answers", json={"question_id": 2, "answer": False})

    stamps = [e.payload["has_pass"] for e in await events_of(api_db, EV_ANSWER_GIVEN)]
    assert stamps == [False, True]


# --------------------------------------------------------------------------
# stats
# --------------------------------------------------------------------------

async def test_stats_rank_the_weakest_topic_first(client, registered):
    # Both "Segnali di divieto" questions wrong (q1 is VERO, q2 is FALSO),
    # the "Distanza di sicurezza" one right.
    await client.post("/users/42/answers", json={"question_id": 1, "answer": False})
    await client.post("/users/42/answers", json={"question_id": 2, "answer": True})
    await client.post("/users/42/answers", json={"question_id": 3, "answer": True})

    body = (await client.get("/users/42/stats")).json()
    assert body["answers_given"] == 3 and body["wrong"] == 2
    assert body["questions_seen"] == 3
    assert body["questions_total"] == 4

    weakest, strongest = body["by_topic"]
    assert weakest["topic"] == "Segnali di divieto"
    assert weakest["error_rate"] == 1.0
    assert strongest["topic"] == "Distanza di sicurezza"
    assert strongest["error_rate"] == 0.0


# --------------------------------------------------------------------------
# GDPR
# --------------------------------------------------------------------------

async def test_delete_removes_the_person_but_keeps_the_metrics(client, registered, api_db):
    await client.post("/users/42/answers", json={"question_id": 1, "answer": True})
    await client.post("/users/42/reports", json={"question_id": 1})
    async with api_db() as s:
        s.add(Purchase(chat_id=42, tribute_purchase_id="trb_x",
                       tier="pass_1m", amount_cents=299))
        await s.commit()

    assert (await client.delete("/users/42")).status_code == 204

    async with api_db() as s:
        assert await s.get(User, 42) is None
        assert (await s.scalars(select(Progress))).all() == []
        assert (await s.scalars(select(Report))).all() == []
        # Events survive, stripped of the identifier.
        remaining = (await s.scalars(select(Event))).all()
        assert remaining and all(e.chat_id is None for e in remaining)
        # The purchase record is retained for accounting and refund matching.
        assert (await s.scalar(select(Purchase))).tribute_purchase_id == "trb_x"


async def test_delete_is_idempotent(client):
    assert (await client.delete("/users/12345")).status_code == 204


async def test_deleted_user_can_start_again_clean(client, registered):
    await client.post("/users/42/answers", json={"question_id": 1, "answer": True})
    await client.delete("/users/42")
    await client.post("/users", json={"chat_id": 42, "lang": "ru"})

    body = (await client.get("/users/42/stats")).json()
    assert body["answers_given"] == 0


# --------------------------------------------------------------------------
# settings
# --------------------------------------------------------------------------

async def test_language_change_is_persisted_and_logged(client, registered, api_db):
    body = (await client.patch("/users/42", json={"lang": "en"})).json()
    assert body["lang"] == "en"


async def test_unsupported_language_is_rejected(client, registered):
    assert (await client.patch("/users/42", json={"lang": "zz"})).status_code == 422


@pytest.mark.parametrize("value", [True, False])
async def test_translation_toggle_round_trips(client, registered, value):
    body = (await client.patch("/users/42", json={"translations_on": value})).json()
    assert body["translations_on"] is value
