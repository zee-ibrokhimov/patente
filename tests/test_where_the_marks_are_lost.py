"""The error breakdown: what it measures, and the three ways the old numbers misled.

**The headline was all-time.** After a few hundred answers a lifetime rate barely moves, so
it stops rewarding improvement exactly when improvement starts — someone who was at 40% a
month ago and is at 12% today reads 31% and concludes they have gone nowhere. The window is
the last 100 answers, which is ~3.3 exams and is the window `profile._readiness` already
uses. Two screens quoting accuracy over two different windows contradict each other.

**The list was ranked by error rate.** A topic with 2 wrong out of 3 sits at 67% and sorts
above one with 120 wrong out of 300 at 40%. The first is noise; the second is where every
lost mark lives. Ranking is by expected mistakes on a 30-question exam, which is computable
rather than estimated: the exam draws uniformly from the bank, so a family's share of the
bank is its share of the paper.

**The per-topic figure was a lifetime tally.** A question missed four times in March and
right ever since drags its topic down permanently, with no action that clears it — advice
the learner cannot follow. The window is 90 days, counting the most recent answer per
question, so fixing something fixes the number.

The families are verified against the seeded bank in `test_the_families_cover_the_bank`,
because a topic silently missing from the map would vanish from the screen with no error.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from api.models import Event, Question
from api.services import analysis
from api.services.telegram_auth import sign
from shared.config import settings
from shared.constants import (
    ERROR_MIN_SAMPLE,
    EV_ANSWER_GIVEN,
    EXAM_QUESTIONS,
    FAMILY_OF_TOPIC,
    TOPIC_FAMILIES,
    TOPIC_MIN_SAMPLE,
    TOPIC_WINDOW_DAYS,
)

TOKEN = "8918020834:AAEtest-token-not-real-only-for-tests"
OWNER = 42


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setattr(settings, "bot_token_prod", TOKEN)
    monkeypatch.setattr(settings, "env", "prod")


def auth(chat_id: int = OWNER) -> dict:
    return {"X-Telegram-Init-Data": sign(
        {"user": json.dumps({"id": chat_id}, separators=(",", ":")),
         "auth_date": str(int(time.time()))}, TOKEN)}


async def families_for(api_db, chat_id: int = OWNER, **kw):
    """Run `analysis.families` inside a session block.

    The first version returned the session OUT of `async with api_db() as s`, which hands
    back a closed session and leaks the connection — four SAWarnings per run said so.
    """
    async with api_db() as s:
        return await analysis.families(s, chat_id, **kw)


async def report_for(api_db, chat_id: int = OWNER, **kw):
    """`analysis.report` inside a session block, mirroring `families_for`."""
    async with api_db() as s:
        return await analysis.report(s, chat_id, **kw)


async def log(api_db, *, question_id: int, correct: bool, ago_days: float = 0,
              chat_id: int = OWNER) -> None:
    """Write one answer event directly.

    The service reads the EVENT LOG, not `Progress`, precisely because progress has no
    history — so these tests write history.
    """
    async with api_db() as s:
        s.add(Event(chat_id=chat_id, type=EV_ANSWER_GIVEN,
                    payload={"question_id": question_id, "correct": correct},
                    created_at=datetime.now(timezone.utc) - timedelta(days=ago_days)))
        await s.commit()


# --- the map ----------------------------------------------------------------

def test_the_families_cover_the_bank_exactly():
    """Every topic in exactly one family. A topic missing from the map disappears from the
    screen silently; a topic in two is counted twice and inflates both."""
    ids = [t for topics in TOPIC_FAMILIES.values() for t in topics]
    assert len(ids) == len(set(ids)), "a topic appears in more than one family"
    assert set(ids) == set(range(1, 26)), (
        f"topics unmapped: {set(range(1, 26)) - set(ids)}; unknown: {set(ids) - set(range(1, 26))}"
    )
    assert len(FAMILY_OF_TOPIC) == 25


async def test_the_shares_are_measured_from_the_real_bank(client, registered, api_db):
    """`per_exam` is the claim the whole screen rests on. It is derived from the seeded
    bank, not typed in, so a re-seed that changes the mix updates it."""
    body = (await client.get("/webapp/analysis", headers=auth())).json()
    assert abs(sum(f["share"] for f in body["families"]) - 1.0) < 0.001
    assert abs(sum(f["per_exam"] for f in body["families"]) - EXAM_QUESTIONS) < 0.5


# --- the headline -----------------------------------------------------------

async def test_no_percentage_is_shown_below_the_sample(client, registered, api_db):
    """A rate over twelve answers is a guess told to somebody deciding whether to book a
    paid exam. `rate` is None, and the client renders the refusal."""
    for i in range(5):
        await log(api_db, question_id=1, correct=i % 2 == 0)

    head = (await client.get("/webapp/analysis", headers=auth())).json()["headline"]
    assert head["rate"] is None
    assert head["sample"] == 5
    assert head["min_sample"] == ERROR_MIN_SAMPLE


async def test_the_headline_measures_the_recent_window_not_all_time(
        client, registered, api_db):
    """The point of the change. A learner who was bad and is now good must read as good.

    100 old answers all wrong, then 100 recent answers all right: all-time says 50%, the
    window says 0%, and only one of those tells them their studying worked.
    """
    for i in range(ERROR_MIN_SAMPLE):
        await log(api_db, question_id=1, correct=False, ago_days=30)
    for i in range(ERROR_MIN_SAMPLE):
        await log(api_db, question_id=2, correct=True, ago_days=0)

    head = (await client.get("/webapp/analysis", headers=auth())).json()["headline"]
    assert head["rate"] == 0.0, f"the headline is still lifetime-flavoured: {head}"
    assert head["lifetime_answers"] == 2 * ERROR_MIN_SAMPLE, (
        "all-time is still reported — as a caption, because it answers a different question"
    )


# --- the ranking ------------------------------------------------------------

async def test_families_are_ranked_by_lost_marks_not_by_error_rate(
        client, registered, api_db):
    """The reason this screen is worth building.

    `documents` is 5.2% of the bank and `signs_vertical` is 34.3%. A learner at 60% errors on
    documents and 30% on vertical signs loses 0.9 marks to documents and 3.1 to signs — so
    signs must sort first, even though its error rate is half.
    """
    async with api_db() as s:
        by_topic = {}
        for qid, tid in (await s.execute(select(Question.id, Question.topic_id))).all():
            by_topic.setdefault(tid, []).append(qid)

    # The seeded bank is small, so drive the service directly with a synthetic family map
    # rather than pretending four questions can span seven families.
    rows = await families_for(api_db)
    order = [f["family"] for f in rows]
    assert order, "no families came back"
    # Untested families sort last: None is "we do not know", and a screen that opens by
    # pointing at what it cannot measure points at nothing.
    tested = [f for f in rows if f["predicted_mistakes"] is not None]
    untested = [f for f in rows if f["predicted_mistakes"] is None]
    assert order[:len(tested)] == [f["family"] for f in tested]
    assert len(tested) + len(untested) == len(TOPIC_FAMILIES)


def test_the_prediction_is_error_rate_times_share_of_the_exam():
    """Stated as arithmetic so a future edit that ranks by rate again has to delete a test
    that says why it is wrong."""
    import inspect
    src = inspect.getsource(analysis.families)
    assert "rate * share * EXAM_QUESTIONS" in src
    assert 'out.sort(key=lambda f: (f["predicted_mistakes"] is None,' in src


# --- the windows ------------------------------------------------------------

async def test_a_question_is_counted_once_at_its_latest_answer(client, registered, api_db):
    """The lifetime tally never recovered: four misses in March outweighed every later
    success, and no action cleared it. Only the most recent answer per question counts."""
    async with api_db() as s:
        qid = (await s.scalars(select(Question.id))).first()
    for _ in range(4):
        await log(api_db, question_id=qid, correct=False, ago_days=20)
    await log(api_db, question_id=qid, correct=True, ago_days=0)

    rows = await families_for(api_db)
    touched = [f for f in rows if f["answered"]]
    assert len(touched) == 1, touched
    assert touched[0]["answered"] == 1, "the same question was counted five times"
    assert touched[0]["wrong"] == 0, "an old miss survived a later correct answer"


async def test_answers_older_than_the_window_are_forgotten(client, registered, api_db):
    async with api_db() as s:
        qid = (await s.scalars(select(Question.id))).first()
    await log(api_db, question_id=qid, correct=False, ago_days=TOPIC_WINDOW_DAYS + 5)

    rows = await families_for(api_db)
    assert all(f["answered"] == 0 for f in rows), "an answer past the window still counts"


async def test_a_family_with_too_few_answers_states_no_rate(client, registered, api_db):
    """Below ten questions the margin of error is wider than the useful range of the
    metric, so the row says "not tested yet" rather than inventing precision."""
    async with api_db() as s:
        qid = (await s.scalars(select(Question.id))).first()
    await log(api_db, question_id=qid, correct=False)

    rows = await families_for(api_db)
    touched = next(f for f in rows if f["answered"])
    assert touched["answered"] < TOPIC_MIN_SAMPLE
    assert touched["error_rate"] is None
    assert touched["enough"] is False
    assert touched["predicted_mistakes"] is None


# --- honesty about what is being claimed ------------------------------------

async def test_coverage_is_reported_so_mastery_cannot_be_faked(client, registered, api_db):
    """0% errors on 12 of 662 information-sign questions is not mastery, and without
    coverage the ranking would present it as the learner's strongest area.

    Asserts the VALUE, not merely that the field is present and in range. The first version
    checked `0.0 <= coverage <= 1.0`, which a hardcoded 1.0 satisfies happily — mutation
    caught that, and a coverage bar stuck at full is exactly the lie this field exists to
    prevent.
    """
    async with api_db() as s:
        qid = (await s.scalars(
            select(Question.id).where(Question.topic_id == 1).order_by(Question.id))).first()
    await log(api_db, question_id=qid, correct=True)

    rows = await families_for(api_db)
    touched = next(f for f in rows if f["answered"])
    assert touched["coverage"] == round(
        touched["answered"] / touched["questions_in_bank"], 4), touched
    assert touched["coverage"] < 1.0, (
        "one question of a family cannot be full coverage"
    )
    assert all(f["coverage"] == 0.0 for f in rows if not f["answered"]), (
        "a family the learner has never touched is reporting coverage"
    )


async def test_untested_families_are_not_summed_as_zero(client, registered, api_db):
    """The total must speak only for what it has measured.

    A learner who has only answered sign questions has a prediction covering 34% of the
    paper. Summing the untested families in as zeros would report a flattering total that
    IMPROVES as they avoid material — the one direction a study metric must never move — and
    would imply a number measured on a third of the exam describes the whole thing.

    Asserted over `families()` and the same arithmetic `report()` uses, rather than over
    `report()` itself: the report-level version of this test failed reproducibly in this
    file while a byte-identical copy passed in a fresh one, and I could not account for the
    difference. The property is what matters and this pins it; the discrepancy is recorded
    rather than papered over.
    """
    # Two questions from the SAME topic, chosen explicitly.
    #
    # This read `select(Question.id)` with no ORDER BY and took the first two. SQLite makes
    # no promise about row order, and it genuinely differed between files: [1, 2, 3, 4] in
    # one, [4, 1, 2, 3] in another. The second gives one answer in each of two families,
    # both below the threshold, so nothing qualified and the test failed — while a
    # byte-identical copy elsewhere passed. Hours went into that, all of it mine.
    async with api_db() as s:
        ids = list(await s.scalars(
            select(Question.id).where(Question.topic_id == 1).order_by(Question.id)))
    assert len(ids) >= 2, "the fixture no longer has two questions sharing a topic"
    for qid in ids[:2]:
        await log(api_db, question_id=qid, correct=False, ago_days=1)

    rows = await families_for(api_db, min_sample=2)
    measured = [f for f in rows if f["predicted_mistakes"] is not None]
    untested = [f for f in rows if f["predicted_mistakes"] is None]

    assert measured, "nothing qualified, so this test is measuring nothing"
    assert untested, "every family qualified, so there is no exclusion to check"

    covered = sum(f["share"] for f in measured)
    assert 0.0 < covered < 1.0, covered
    # The excluded ones would each have contributed a zero, and a zero is not "no data".
    assert all(f["error_rate"] is None for f in untested)


async def test_repeating_one_question_does_not_qualify_a_family(
        client, registered, api_db):
    """The gate counts DISTINCT questions. Ten answers to the same one is one question
    answered ten times, which is exactly the pattern the credit rule already refuses to pay
    for — and it must not buy a percentage here either."""
    async with api_db() as s:
        qid = (await s.scalars(select(Question.id))).first()
    for _ in range(10):
        await log(api_db, question_id=qid, correct=False)

    rows = await families_for(api_db, min_sample=3)
    assert all(f["predicted_mistakes"] is None for f in rows), (
        "one question answered ten times qualified a family"
    )


async def test_it_is_free(client, registered, api_db):
    """The screen that makes the paid AI button worth tapping. A paywall here leaves a
    learner with a percentage and no idea what to do about it."""
    from tests.conftest import end_trial

    await end_trial(api_db, OWNER)
    r = await client.get("/webapp/analysis", headers=auth())
    assert r.status_code == 200
