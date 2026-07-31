"""Practice serves what you got wrong. It did not, and nothing noticed for weeks.

Every answer wrote a Leitner box and a due date into `progress`. Nothing ever read them
back: the only reader was `selection.next_question`, reachable solely from the loopback
route the bot used BEFORE quizzing moved into the Mini App. Practice drew from
`exam_paper` — ORDER BY random() over all 7106 questions.

So the app recorded spaced repetition and practised uniform random. A learner answered a
question wrong, it was scheduled to return in ten minutes, and it never came back. In
production: 15 progress rows across boxes 1 and 2, written and never used.

Every test here would have passed before the fix if it only checked that practice returns
questions — which is why they check WHICH questions.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from api.models import Progress, Question, Quesito
from api.services import selection
from api.services.entitlement import evaluate
from shared.constants import EXAM_QUESTIONS, MODE_EXAM, MODE_PRACTICE

NOW = datetime.now(timezone.utc)


@pytest.fixture
async def bank(api_db):
    """Sixty questions, so a 30-item paper cannot accidentally contain everything."""
    async with api_db() as s:
        s.add(Quesito(id=400, topic_id=1, primary_image=None))
        await s.flush()
        s.add_all([
            Question(id=2000 + i, quesito_id=400, topic_id=1, cluster_id=1,
                     statement_it=f"Affermazione {i}", answer=i % 2 == 0,
                     source_version="v1")
            for i in range(60)
        ])
        await s.commit()
    return api_db


async def user_with(api_db, chat_id: int, due_ids: list[int], when: datetime):
    from api.models import User
    async with api_db() as s:
        user = await s.get(User, chat_id)
        if user is None:
            user = User(chat_id=chat_id, lang="ru",
                        pass_expires_at=NOW + timedelta(days=30))
            s.add(user)
            await s.flush()
        else:
            user.pass_expires_at = NOW + timedelta(days=30)
        for qid in due_ids:
            s.add(Progress(chat_id=chat_id, question_id=qid, box=1, due_at=when,
                           seen=1, wrong=1))
        await s.commit()
    async with api_db() as s:
        return await s.get(User, chat_id)


# --- the fix ----------------------------------------------------------------

async def test_practice_serves_what_is_due_before_anything_else(bank):
    """THE test. Five questions answered wrong and now due; a practice batch must lead
    with those five rather than five of the other fifty-five at random."""
    overdue = [2001, 2002, 2003, 2004, 2005]
    user = await user_with(bank, 501, overdue, NOW - timedelta(hours=1))
    async with bank() as s:
        paper = await selection.practice_paper(s, user, evaluate(user), 10)
    first_five = {q.id for q in paper[:5]}
    assert first_five == set(overdue), f"due questions were not served first: {first_five}"


async def test_a_question_answered_wrong_comes_back(bank):
    """Stated the way a learner would: I got this wrong, will I see it again?

    Before the fix the answer was no — a 1-in-7106 chance per draw."""
    user = await user_with(bank, 502, [2042], NOW - timedelta(minutes=20))
    async with bank() as s:
        paper = await selection.practice_paper(s, user, evaluate(user), 30)
    assert 2042 in {q.id for q in paper}


async def test_practice_falls_back_to_new_material(bank):
    """Nothing due yet: a beginner must still get a full batch."""
    user = await user_with(bank, 503, [], NOW)
    async with bank() as s:
        paper = await selection.practice_paper(s, user, evaluate(user), 20)
    assert len(paper) == 20


async def test_practice_revises_early_rather_than_running_out(bank):
    """Everything seen, nothing due. A short session that stops is worse than one that
    revises slightly ahead of schedule."""
    ids = list(range(2000, 2060))
    user = await user_with(bank, 504, ids, NOW + timedelta(days=3))
    async with bank() as s:
        paper = await selection.practice_paper(s, user, evaluate(user), 15)
    assert len(paper) == 15


async def test_a_batch_never_repeats_a_question(bank):
    user = await user_with(bank, 505, [2001, 2002], NOW - timedelta(hours=1))
    async with bank() as s:
        paper = await selection.practice_paper(s, user, evaluate(user), 30)
    ids = [q.id for q in paper]
    assert len(ids) == len(set(ids))


async def test_extending_does_not_re_serve_the_current_paper(bank):
    """Practice extends the same sitting, so the second batch must exclude the first."""
    user = await user_with(bank, 506, [], NOW)
    async with bank() as s:
        first = await selection.practice_paper(s, user, evaluate(user), 20)
        used = {q.id for q in first}
        second = await selection.practice_paper(s, user, evaluate(user), 20, exclude=used)
    assert not ({q.id for q in second} & used)


# --- what must NOT change ---------------------------------------------------

async def test_the_exam_is_still_a_uniform_random_draw(bank):
    """Load a user with 30 overdue questions, then draw an exam. If the exam favoured
    them the score would be systematically pessimistic and would mean nothing — the whole
    argument in exam_paper's docstring."""
    overdue = list(range(2000, 2030))
    user = await user_with(bank, 507, overdue, NOW - timedelta(days=1))
    async with bank() as s:
        paper = await selection.exam_paper(s, EXAM_QUESTIONS)
    overlap = len({q.id for q in paper} & set(overdue))
    # 30 of 64 questions are overdue, so a uniform draw lands near 14. Anything close to
    # 30 would mean the exam had started teaching instead of measuring.
    assert overlap < 28, f"the exam drew {overlap}/30 from the due set — it is not uniform"


async def test_a_free_user_still_gets_practice(bank):
    """Spaced repetition may be gated; a batch of questions is not. A free learner must
    never get an empty paper."""
    from api.models import User
    async with bank() as s:
        s.add(User(chat_id=508, lang="ru", pass_expires_at=None))
        await s.commit()
        user = await s.get(User, 508)
        paper = await selection.practice_paper(s, user, evaluate(user), 20)
    assert len(paper) == 20


async def test_a_real_practice_SESSION_serves_due_questions(client, api_db, bank):
    """Through the actual endpoint, not the selector.

    Without this the wiring could be reverted — `create()` calling exam_paper again —
    and every test above would still pass, because they call practice_paper directly.
    That is exactly how the original bug survived: the Leitner selector was correct and
    simply nobody called it.
    """
    import json as _json
    import time as _time

    from api.services.telegram_auth import sign
    from shared.config import settings

    TOKEN = "8918020834:AAEtest-token-not-real-only-for-tests"
    settings.bot_token_prod = TOKEN
    settings.env = "prod"
    OWNER = 42

    def auth(chat_id=OWNER):
        return {"X-Telegram-Init-Data": sign(
            {"user": _json.dumps({"id": chat_id}, separators=(",", ":")),
             "auth_date": str(int(_time.time()))}, TOKEN)}

    overdue = [2010, 2011, 2012]
    await user_with(bank, OWNER, overdue, NOW - timedelta(hours=2))

    r = await client.post("/webapp/sessions", headers=auth(), json={"mode": "practice"})
    assert r.status_code == 200
    served = [q["id"] for q in r.json()["questions"]]

    # POSITION, not membership. Asserting only that the three appear somewhere in a
    # 30-of-64 draw caught the reverted wiring just 4 times out of 6 — random lands on
    # them often enough by chance. A flaky guard is worse than none: it passes often
    # enough to let the bug back in. Due questions are served FIRST, which a uniform
    # draw reproduces with probability about 1 in 250,000.
    assert served[:3] == overdue or set(served[:3]) == set(overdue), (
        f"due questions were not at the front of the paper: {served[:6]}"
    )
