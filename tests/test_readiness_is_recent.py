"""Readiness must describe how the learner is doing NOW.

It is the one number on the profile that makes a claim about the future: whether to book a
legally required exam that costs money and a re-sit to fail. The module docstring has always
promised the right thing — "accuracy over recent answers, not over all time. Someone who was
at 40% a month ago and is at 85% now is ready; an all-time average says otherwise and is
useless." The code did the opposite.

It summed `progress.seen` and `progress.wrong` — per-question LIFETIME running totals — over
the 100 most recently touched questions. Three consequences, all measured by the audit:

  · it can essentially never fall, because mistakes the learner has since FIXED keep
    contributing forever and no action clears them;
  · it can go UP after a wrong answer (0.748 -> 0.754), because answering reorders the
    window and can evict a question with a worse lifetime ratio;
  · somebody back after two months who has just got 30 of 30 wrong still reads 94%,
    because ninety-odd stale totals outvote today's thirty answers.

`events` has carried one row per answer since the first commit, with `correct` in the
payload and an index on (chat_id, created_at). The real rolling window was already on disk.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from api.models import Event, User
from api.services import profile
from api.services.profile import MIN_SAMPLE, RECENT_WINDOW, STALE_AFTER
from shared.constants import EV_ANSWER_GIVEN

NOW = datetime.now(timezone.utc)
CHAT = 42


async def answers(api_db, chat_id, results, *, at=None, graded=True):
    """Write `results` (a list of bools) as answer events, oldest first."""
    at = at or NOW
    async with api_db() as s:
        for i, correct in enumerate(results):
            s.add(Event(
                chat_id=chat_id,
                type=EV_ANSWER_GIVEN,
                payload={"correct": bool(correct), "graded": graded, "question_id": i},
                created_at=at + timedelta(seconds=i),
            ))
        await s.commit()


async def readiness(api_db, chat_id=CHAT, now=None):
    async with api_db() as s:
        return await profile._readiness(s, chat_id, now=now or NOW + timedelta(minutes=5))


# --- the three measured failures --------------------------------------------

async def test_recent_form_beats_ancient_mistakes(api_db, registered):
    """"Someone who was at 40% a month ago and is at 85% now is ready" — the docstring's
    own promise, which the old implementation could not keep."""
    await answers(api_db, CHAT, [False] * 100, at=NOW - timedelta(days=30))
    await answers(api_db, CHAT, [True] * 100, at=NOW - timedelta(hours=1))

    value, sample = await readiness(api_db)
    assert sample == RECENT_WINDOW
    assert value == 1.0, f"a month of fixed mistakes still dragged readiness to {value}"


async def test_readiness_falls_after_a_bad_run(api_db, registered):
    """It must be able to go DOWN. The old one essentially could not."""
    await answers(api_db, CHAT, [True] * 100, at=NOW - timedelta(days=2))
    before, _ = await readiness(api_db)

    await answers(api_db, CHAT, [False] * 30, at=NOW - timedelta(minutes=10))
    after, _ = await readiness(api_db)

    assert before == 1.0
    assert after < before, f"30 wrong answers moved readiness from {before} to {after}"
    assert after == pytest.approx(0.7, abs=0.01)


async def test_a_wrong_answer_never_raises_readiness(api_db, registered):
    """The measured absurdity: 0.748 -> 0.754 after getting one wrong, because answering
    reordered the window rather than adding to it."""
    await answers(api_db, CHAT, [True] * 75 + [False] * 25, at=NOW - timedelta(hours=3))
    before, _ = await readiness(api_db)

    await answers(api_db, CHAT, [False], at=NOW - timedelta(minutes=1))
    after, _ = await readiness(api_db)

    assert after <= before, f"getting one wrong moved readiness UP: {before} -> {after}"


async def test_someone_back_after_two_months_is_not_told_they_are_ready(api_db, registered):
    """THE dangerous case, verbatim from the audit: a learner returns after a long gap,
    gets 30 of 30 wrong, and the gauge sits green at 94% past the pass tick with "You are
    above the threshold" — so they book the real exam."""
    await answers(api_db, CHAT, [True] * 94 + [False] * 6, at=NOW - timedelta(days=200))
    await answers(api_db, CHAT, [False] * 30, at=NOW - timedelta(minutes=5))

    value, sample = await readiness(api_db)
    assert sample == 30, "answers from 200 days ago were still counted"
    assert value is None, \
        f"readiness reported {value} to someone who just got 30 of 30 wrong"


# --- refusing to answer -----------------------------------------------------

async def test_below_the_minimum_it_says_nothing(api_db, registered):
    await answers(api_db, CHAT, [True] * (MIN_SAMPLE - 1))
    value, sample = await readiness(api_db)
    assert value is None
    assert sample == MIN_SAMPLE - 1


async def test_at_the_minimum_it_answers(api_db, registered):
    await answers(api_db, CHAT, [True] * MIN_SAMPLE)
    value, sample = await readiness(api_db)
    assert value == 1.0
    assert sample == MIN_SAMPLE


async def test_stale_answers_expire_rather_than_linger(api_db, registered):
    """Past the cutoff they stop counting, and if too few remain the gauge goes back to
    saying nothing — the honest output for "I do not know how you are doing now"."""
    await answers(api_db, CHAT, [True] * 200, at=NOW - STALE_AFTER - timedelta(days=1))
    value, sample = await readiness(api_db)
    assert (value, sample) == (None, 0)


async def test_a_new_user_has_no_readiness(api_db, registered):
    assert await readiness(api_db) == (None, 0)


# --- the sample size the client shows ---------------------------------------

async def test_the_sample_is_a_count_of_ANSWERS(api_db, registered):
    """`readiness_sample` is rendered to the user as what the number is based on. It used
    to be a sum of lifetime `seen` across questions, which could exceed the number of
    answers in the window and had no meaning the user could act on."""
    await answers(api_db, CHAT, [True] * 40)
    _, sample = await readiness(api_db)
    assert sample == 40


async def test_the_window_is_capped(api_db, registered):
    await answers(api_db, CHAT, [True] * (RECENT_WINDOW * 3))
    _, sample = await readiness(api_db)
    assert sample == RECENT_WINDOW


async def test_one_users_answers_never_reach_another(api_db, registered):
    async with api_db() as s:
        s.add(User(chat_id=99, lang="ru"))
        await s.commit()
    await answers(api_db, 99, [False] * 200)
    assert await readiness(api_db, CHAT) == (None, 0)


# --- exam answers count -----------------------------------------------------

async def test_exam_answers_count_toward_readiness(api_db, registered):
    """An exam is the most representative sample of exam performance there is. Excluding
    it would mean the activity closest to the real thing had no effect on the estimate.

    It stays out of the Leitner SCHEDULE — a separate concern about what to teach next.
    """
    await answers(api_db, CHAT, [False] * 100, graded=False)
    value, sample = await readiness(api_db)
    assert sample == 100
    assert value == 0.0, "a failed mock exam left readiness untouched"


async def test_practice_and_exam_answers_are_pooled(api_db, registered):
    await answers(api_db, CHAT, [True] * 50, at=NOW - timedelta(hours=2), graded=True)
    await answers(api_db, CHAT, [False] * 50, at=NOW - timedelta(hours=1), graded=False)
    value, sample = await readiness(api_db)
    assert sample == 100
    assert value == 0.5
