"""Starting over, without losing what you paid for.

A learner who has drilled for a month and wants a clean readiness score, or who is handing
the app to a friend, had no way to clear their history. Everything was permanent.

The line that matters most is what a reset does NOT touch. Wiping progress must never cost
someone money they have paid — not the pass, not the purchase rows a refund would later
need to find. A destructive feature that quietly cancels a subscription is a refund
request and a bad review, and it would be entirely our fault.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from api.models import (
    Event,
    Progress,
    Purchase,
    QuizSession,
    QuizSessionItem,
    User,
    VocabProgress,
    VocabTerm,
)
from api.services.telegram_auth import sign
from shared.config import settings
from shared.constants import EV_PROGRESS_RESET

TOKEN = "8918020834:AAEtest-token-not-real-only-for-tests"
OWNER = 42
NOW = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setattr(settings, "bot_token_prod", TOKEN)
    monkeypatch.setattr(settings, "env", "prod")


def auth(chat_id: int = OWNER) -> dict:
    return {"X-Telegram-Init-Data": sign(
        {"user": json.dumps({"id": chat_id}, separators=(",", ":")),
         "auth_date": str(int(time.time()))}, TOKEN)}


@pytest.fixture
async def busy_learner(client, registered, api_db):
    """Someone with a month behind them AND a subscription."""
    async with api_db() as s:
        user = await s.get(User, OWNER)
        user.pass_expires_at = NOW + timedelta(days=60)
        user.lang = "uz"
        user.translations_on = False
        user.channel_status = "member"
        s.add_all([
            Progress(chat_id=OWNER, question_id=1, box=3, due_at=NOW, seen=9, wrong=2),
            Progress(chat_id=OWNER, question_id=2, box=1, due_at=NOW, seen=4, wrong=4),
        ])
        s.add(Purchase(chat_id=OWNER, tribute_purchase_id="paid-1", tier="pass_3m",
                       amount_cents=799, currency="eur", extended_to=NOW))
        s.add(VocabTerm(rank=1, it="sosta", en="parking", ru="стоянка", uz="turish"))
        await s.flush()
        s.add(VocabProgress(chat_id=OWNER, term_id=1, box=4, due_at=NOW, seen=7))
        sitting = QuizSession(chat_id=OWNER, mode="exam", state="submitted",
                              started_at=NOW, question_count=30, answered=30, wrong=2,
                              max_errors=3, passed=True)
        s.add(sitting)
        await s.flush()
        s.add(QuizSessionItem(session_id=sitting.id, ordinal=1, question_id=1))
        await s.commit()
    return api_db


# --- what it must NOT do ----------------------------------------------------

async def test_a_reset_never_costs_the_subscription(client, busy_learner):
    """THE test. Everything else here is bookkeeping; this one is money."""
    await client.post("/webapp/reset", headers=auth())
    async with busy_learner() as s:
        user = await s.get(User, OWNER)
        assert user.pass_expires_at is not None
        assert user.pass_expires_at > NOW


async def test_purchase_rows_survive(client, busy_learner):
    """A refund arriving after a reset still has to find its row, or the money is
    unrefundable and the pass unrevokable."""
    await client.post("/webapp/reset", headers=auth())
    async with busy_learner() as s:
        rows = (await s.scalars(select(Purchase).where(Purchase.chat_id == OWNER))).all()
    assert len(rows) == 1


async def test_settings_are_not_progress(client, busy_learner):
    """Language and the translation switch are preferences. Resetting them would make
    an Uzbek learner's app silently revert to Russian."""
    await client.post("/webapp/reset", headers=auth())
    async with busy_learner() as s:
        user = await s.get(User, OWNER)
    assert user.lang == "uz"
    assert user.translations_on is False


async def test_channel_membership_is_not_forgotten(client, busy_learner):
    """It is Telegram's fact, not ours. Clearing it would drop Premium for a channel
    member until the next refresh."""
    await client.post("/webapp/reset", headers=auth())
    async with busy_learner() as s:
        assert (await s.get(User, OWNER)).channel_status == "member"


async def test_the_event_log_is_not_rewritten(client, busy_learner):
    """Append-only. Deleting history to make a reset look tidy would corrupt the only
    honest record of what happened."""
    async with busy_learner() as s:
        before = await s.scalar(select(func.count()).select_from(Event))
    await client.post("/webapp/reset", headers=auth())
    async with busy_learner() as s:
        after = await s.scalar(select(func.count()).select_from(Event))
    assert after > before, "the reset should ADD an event, never remove any"


# --- what it must do --------------------------------------------------------

async def test_question_progress_is_gone(client, busy_learner):
    await client.post("/webapp/reset", headers=auth())
    async with busy_learner() as s:
        rows = (await s.scalars(select(Progress).where(Progress.chat_id == OWNER))).all()
    assert rows == []


async def test_vocabulary_progress_is_gone(client, busy_learner):
    await client.post("/webapp/reset", headers=auth())
    async with busy_learner() as s:
        rows = (await s.scalars(
            select(VocabProgress).where(VocabProgress.chat_id == OWNER))).all()
    assert rows == []


async def test_sittings_and_their_items_are_gone(client, busy_learner):
    """Items too. Leaving them would keep a readiness figure computed from answers whose
    questions no longer exist in anyone's history."""
    await client.post("/webapp/reset", headers=auth())
    async with busy_learner() as s:
        assert (await s.scalars(
            select(QuizSession).where(QuizSession.chat_id == OWNER))).all() == []
        assert (await s.scalars(select(QuizSessionItem))).all() == []


async def test_the_reset_is_recorded(client, busy_learner):
    """"My stats vanished" is a support message someone will send after forgetting they
    pressed this."""
    await client.post("/webapp/reset", headers=auth())
    async with busy_learner() as s:
        kinds = [e.type for e in (await s.scalars(select(Event))).all()]
    assert EV_PROGRESS_RESET in kinds


# --- the confirmation -------------------------------------------------------

async def test_the_preview_counts_what_would_go(client, busy_learner):
    """"This will delete your progress" is clicked past. "13 answers and 1 exam" is read."""
    body = (await client.get("/webapp/reset/preview", headers=auth())).json()
    assert body["answers"] == 13      # 9 + 4
    assert body["questions"] == 2
    assert body["sittings"] == 1
    assert body["words"] == 1


async def test_the_preview_changes_nothing(client, busy_learner):
    await client.get("/webapp/reset/preview", headers=auth())
    async with busy_learner() as s:
        assert (await s.scalars(select(Progress).where(Progress.chat_id == OWNER))).all()


async def test_resetting_twice_is_harmless(client, busy_learner):
    await client.post("/webapp/reset", headers=auth())
    r = await client.post("/webapp/reset", headers=auth())
    assert r.status_code == 200
    assert r.json()["answers"] == 0


# --- whose progress ---------------------------------------------------------

async def test_one_user_cannot_reset_another(client, busy_learner):
    """There is no id in the request — the caller is whoever the signature proves — so
    this is structural rather than a check that could be forgotten. Pinned anyway."""
    await client.post("/webapp/reset", headers=auth(99))
    async with busy_learner() as s:
        rows = (await s.scalars(select(Progress).where(Progress.chat_id == OWNER))).all()
    assert len(rows) == 2, "another user's reset destroyed this learner's progress"


async def test_reset_requires_a_signature(client, busy_learner):
    assert (await client.post("/webapp/reset")).status_code == 401
    assert (await client.get("/webapp/reset/preview")).status_code == 401
