"""A day on the streak costs ten distinct questions, and the calendar is Rome's.

The old rule was "answered something", which one tap satisfied and which made the number on
the profile mean nothing. Everything here is about the four ways the new rule can be cheated
or can accidentally punish somebody who did nothing wrong:

  · ten answers to the SAME question is one question, not ten;
  · answers given too fast to be reading are recorded but do not count;
  · midnight is Rome's midnight, not the server's;
  · ten at 23:58 and ten at 00:01 is one sitting, not two days.

The fourth is the one with money attached: every fourteenth day pays out three days of
Premium, so a rule that lets a learner bank two days in four minutes pays out twice as fast
as it should, forever.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

import pytest

from api.models import Event, StreakDay, User
from api.services import streak
from api.services.streak import GOAL, MIN_GAP, ROME
from shared.constants import EV_ANSWER_GIVEN, EV_STREAK_MILESTONE

CHAT = 42


def rome(day: date, hour: int, minute: int = 0) -> datetime:
    """A Rome wall-clock moment, as the UTC instant everything is stored in."""
    return datetime.combine(day, time(hour, minute), tzinfo=ROME).astimezone(timezone.utc)


async def answer(api_db, when: datetime, question_id: int, credited: bool = True,
                 chat_id: int = CHAT) -> bool:
    """One answer, then the streak's own look at it. Returns whether it earned the day."""
    async with api_db() as s:
        s.add(Event(chat_id=chat_id, type=EV_ANSWER_GIVEN, created_at=when,
                    payload={"question_id": question_id, "correct": True,
                             "credited": credited}))
        await s.flush()
        earned = await streak.note_answer(s, chat_id, when)
        await s.commit()
    return earned


async def answer_many(api_db, start: datetime, count: int, first_id: int = 1,
                      step: timedelta = timedelta(seconds=10), **kw) -> bool:
    earned = False
    for i in range(count):
        earned = await answer(api_db, start + step * i, first_id + i, **kw) or earned
    return earned


async def days_of(api_db, chat_id: int = CHAT) -> list[str]:
    async with api_db() as s:
        return sorted(d.isoformat() for d in await streak.qualifying_days(s, chat_id))


# --- the goal ---------------------------------------------------------------

async def test_one_answer_short_is_not_a_day(api_db, registered):
    """The bar is a bar. Nine questions is a good effort and not a streak day, and the
    profile has to be able to say which."""
    earned = await answer_many(api_db, rome(date(2026, 7, 31), 12), GOAL - 1)
    assert await days_of(api_db) == []
    assert not earned, \
        "an answer below the goal reported earning the day — the client celebrates on this"


async def test_the_tenth_question_earns_the_day(api_db, registered):
    earned = await answer_many(api_db, rome(date(2026, 7, 31), 12), GOAL)
    assert await days_of(api_db) == ["2026-07-31"]
    assert earned, "the answer that completed the goal did not say so"


async def test_only_the_answer_that_earned_it_says_so(api_db, registered):
    """`streak_earned_today` is what the client congratulates on. Returning True for every
    answer after the tenth would fire the celebration on all of them."""
    day = rome(date(2026, 7, 31), 12)
    await answer_many(api_db, day, GOAL)
    again = await answer(api_db, day + timedelta(minutes=5), 999)
    assert not again


async def test_the_same_question_ten_times_is_not_ten_questions(api_db, registered):
    """The scheduler re-serves a missed question after ten minutes, so without this the
    daily goal is a sixty-second loop on one card."""
    start = rome(date(2026, 7, 31), 12)
    for i in range(GOAL * 3):
        await answer(api_db, start + timedelta(seconds=i * 10), question_id=7)
    assert await days_of(api_db) == []


async def test_answers_too_fast_to_be_reading_do_not_count(api_db, registered):
    """`credited` is stamped by pacing at write time. A day made of uncredited answers is a
    script, and the whole point of the flag is that it reaches the things that pay out."""
    await answer_many(api_db, rome(date(2026, 7, 31), 12), GOAL, credited=False)
    assert await days_of(api_db) == []


async def test_answers_from_before_pacing_existed_still_count(api_db, registered):
    """No `credited` key at all means the event predates the rule. Treating those as
    uncredited would delete the history of everyone who was here first."""
    async with api_db() as s:
        for i in range(GOAL):
            s.add(Event(chat_id=CHAT, type=EV_ANSWER_GIVEN,
                        created_at=rome(date(2026, 7, 31), 12) + timedelta(seconds=i * 10),
                        payload={"question_id": i, "correct": True}))
        await s.flush()
        await streak.note_answer(s, CHAT, rome(date(2026, 7, 31), 13))
        await s.commit()
    assert await days_of(api_db) == ["2026-07-31"]


async def test_one_learners_questions_are_not_anothers(api_db, registered):
    """Nine of mine plus nine of yours is not a day for either of us."""
    day = rome(date(2026, 7, 31), 12)
    await answer_many(api_db, day, GOAL - 1, first_id=1, chat_id=CHAT)
    await answer_many(api_db, day, GOAL - 1, first_id=100, chat_id=999)
    assert await days_of(api_db, CHAT) == []
    assert await days_of(api_db, 999) == []


# --- the calendar is Rome's -------------------------------------------------

async def test_a_sitting_across_the_servers_midnight_is_still_one_day(api_db, registered):
    """01:50 to 02:10 Rome — one sitting, and one that straddles UTC midnight, because in
    summer the UTC day rolls over at 02:00 Rome.

    Under a UTC day boundary this is five questions in each of two days and neither of them
    counts, with nothing on screen to explain why. Under Rome's calendar it is one late
    night, which is what it is.
    """
    night = rome(date(2026, 8, 1), 1, 50)
    assert night.astimezone(timezone.utc).date() != (
        night + timedelta(minutes=20)).astimezone(timezone.utc).date(), \
        "this test is only meaningful while the sitting straddles UTC midnight"
    await answer_many(api_db, night, GOAL, step=timedelta(minutes=2))
    assert await days_of(api_db) == ["2026-08-01"], \
        "the sitting was split across the server's midnight instead of Rome's"


async def test_the_day_a_learner_is_in_is_the_day_they_are_in(api_db, registered):
    """01:00 Rome on 1 August is 23:00 UTC on 31 July. The learner's calendar wins."""
    await answer_many(api_db, rome(date(2026, 8, 1), 1), GOAL)
    assert await days_of(api_db) == ["2026-08-01"]


async def test_a_rome_day_is_counted_end_to_end(api_db, registered):
    """Half the goal just after midnight and half just before, in Rome. One day.

    This is the case that fails if the day is measured by adding 24 hours to a UTC instant
    rather than by localising midnight — the last hour would fall outside the window.
    """
    day = date(2026, 8, 1)
    await answer_many(api_db, rome(day, 0, 5), GOAL // 2, first_id=1)
    await answer_many(api_db, rome(day, 23, 30), GOAL // 2, first_id=50)
    assert await days_of(api_db) == [day.isoformat()]


async def test_the_clocks_going_back_does_not_lose_a_day(api_db, registered):
    """25 October 2026 is 25 hours long in Rome — 02:00 happens twice. A window built by
    adding a fixed day would end an hour early and drop the last hour of study."""
    day = date(2026, 10, 25)
    await answer_many(api_db, rome(day, 1, 0), GOAL // 2, first_id=1)
    await answer_many(api_db, rome(day, 23, 30), GOAL // 2, first_id=50)
    assert await days_of(api_db) == [day.isoformat()]


# --- the minimum gap --------------------------------------------------------

async def test_two_sittings_either_side_of_midnight_are_one_day(api_db, registered):
    """THE farm. Ten at 23:58 and ten at 00:01 would otherwise bank two days for four
    minutes of work — every other night, forever, paying out Premium at twice the rate."""
    await answer_many(api_db, rome(date(2026, 7, 31), 23, 58), GOAL,
                      first_id=1, step=timedelta(seconds=5))
    await answer_many(api_db, rome(date(2026, 8, 1), 0, 1), GOAL,
                      first_id=100, step=timedelta(seconds=5))
    assert await days_of(api_db) == ["2026-07-31"], \
        "a four-minute burst across midnight banked two days"


async def test_the_second_day_is_earned_once_the_gap_has_passed(api_db, registered):
    """Not forfeited — deferred. Someone who genuinely studies twice in a night gets the
    second day when they come back, rather than being locked out of it for finishing early.
    """
    await answer_many(api_db, rome(date(2026, 7, 31), 23, 58), GOAL, first_id=1)
    await answer_many(api_db, rome(date(2026, 8, 1), 0, 1), GOAL, first_id=100)
    assert await days_of(api_db) == ["2026-07-31"]

    # Later that day, past the gap. One more question is enough — the ten are already in.
    earned = await answer(api_db, rome(date(2026, 8, 1), 12), 200)
    assert earned
    assert await days_of(api_db) == ["2026-07-31", "2026-08-01"]


async def test_a_night_shift_and_a_morning_are_still_two_days(api_db, registered):
    """The gap must not punish real lives. Finishing at 23:00 and starting again at 08:00
    is nine hours, and those are two days by any honest reading."""
    await answer_many(api_db, rome(date(2026, 7, 31), 22, 30), GOAL, first_id=1)
    await answer_many(api_db, rome(date(2026, 8, 1), 8, 0), GOAL, first_id=100)
    assert await days_of(api_db) == ["2026-07-31", "2026-08-01"]


async def test_the_gap_is_measured_between_qualifying_moments(api_db, registered):
    """Not between calendar days, and not from the first answer of a day. It is the instant
    the day was EARNED that starts the clock."""
    async with api_db() as s:
        await streak.note_answer(s, CHAT, rome(date(2026, 7, 31), 12))
        assert await streak.last_qualified_at(s, CHAT) is None
    await answer_many(api_db, rome(date(2026, 7, 31), 12), GOAL)
    async with api_db() as s:
        earned_at = await streak.last_qualified_at(s, CHAT)
    assert earned_at == rome(date(2026, 7, 31), 12) + timedelta(seconds=(GOAL - 1) * 10)
    assert MIN_GAP == timedelta(hours=8)


# --- what the profile shows ---------------------------------------------------

async def test_progress_toward_today_is_visible_before_the_day_is_earned(api_db, registered):
    """A goal nobody can see themselves approaching is a goal they discover by failing it."""
    now = rome(date(2026, 7, 31), 12)
    await answer_many(api_db, now, 4)
    async with api_db() as s:
        assert await streak.counted_today(s, CHAT, now + timedelta(hours=1)) == 4


async def test_yesterdays_work_is_not_todays_progress(api_db, registered):
    await answer_many(api_db, rome(date(2026, 7, 30), 12), GOAL)
    async with api_db() as s:
        assert await streak.counted_today(s, CHAT, rome(date(2026, 7, 31), 9)) == 0


async def test_the_profile_sends_the_goal_rather_than_the_client_knowing_it(client,
                                                                           registered):
    """Two copies of a product rule disagree the first time one is tuned."""
    import json
    import time as clock

    from api.services.telegram_auth import sign
    from shared.config import settings

    token = "8918020834:AAEtest-token-not-real-only-for-tests"
    settings.bot_token_prod = token
    settings.env = "prod"
    headers = {"X-Telegram-Init-Data": sign(
        {"user": json.dumps({"id": 42}, separators=(",", ":")),
         "auth_date": str(int(clock.time()))}, token)}

    body = (await client.get("/webapp/profile", headers=headers)).json()
    assert body["streak_goal"] == GOAL
    assert body["streak_today"] == 0


# --- the fourteen-day payout --------------------------------------------------

async def _fortnight(api_db, first: date, days: int = 14, chat_id: int = CHAT):
    from tests.conftest import studied_on
    await studied_on(api_db, chat_id, [first + timedelta(days=n) for n in range(days)])


async def test_a_fortnight_pays_three_days_of_premium(api_db, registered):
    start = date(2026, 7, 1)
    await _fortnight(api_db, start)
    async with api_db() as s:
        user = await s.get(User, CHAT)
        assert user.pass_expires_at is None
        days, _freezes, granted = await streak.refresh(
            s, user, now=rome(start + timedelta(days=13), 20))
        await s.commit()
    assert days == 14
    assert granted == streak.MILESTONE_DAYS
    async with api_db() as s:
        assert (await s.get(User, CHAT)).pass_expires_at is not None


async def test_thirteen_days_pays_nothing(api_db, registered):
    start = date(2026, 7, 1)
    await _fortnight(api_db, start, days=13)
    async with api_db() as s:
        user = await s.get(User, CHAT)
        _days, _freezes, granted = await streak.refresh(
            s, user, now=rome(start + timedelta(days=12), 20))
        await s.commit()
    assert granted == 0
    async with api_db() as s:
        assert (await s.get(User, CHAT)).pass_expires_at is None


async def test_opening_the_profile_twice_does_not_pay_twice(api_db, registered):
    """The profile is read on every visit. A payout that is not idempotent is a subscription
    granted per page view."""
    start = date(2026, 7, 1)
    await _fortnight(api_db, start)
    now = rome(start + timedelta(days=13), 20)
    async with api_db() as s:
        user = await s.get(User, CHAT)
        first = await streak.refresh(s, user, now=now)
        expiry = user.pass_expires_at
        for _ in range(4):
            _d, _f, granted = await streak.refresh(s, user, now=now)
            assert granted == 0
        await s.commit()
    assert first[2] == streak.MILESTONE_DAYS
    async with api_db() as s:
        assert (await s.get(User, CHAT)).pass_expires_at == expiry
        paid = await streak.paid_milestones(s, CHAT)
    assert len(paid) == 1


async def test_a_milestone_missed_on_the_day_is_still_paid_later(api_db, registered):
    """The fourteenth day might be a Sunday they never opened the app on. Paying only for a
    milestone reached exactly today would silently never pay them at all."""
    start = date(2026, 7, 1)
    await _fortnight(api_db, start, days=20)
    async with api_db() as s:
        user = await s.get(User, CHAT)
        _d, _f, granted = await streak.refresh(
            s, user, now=rome(start + timedelta(days=19), 20))
        await s.commit()
    assert granted == streak.MILESTONE_DAYS
    async with api_db() as s:
        assert await streak.paid_milestones(s, CHAT) == {start + timedelta(days=13)}


async def test_twenty_eight_days_pays_twice(api_db, registered):
    """Recurring, by the owner's decision. Reaching the second milestone pays again."""
    start = date(2026, 7, 1)
    await _fortnight(api_db, start, days=28)
    async with api_db() as s:
        user = await s.get(User, CHAT)
        _d, _f, granted = await streak.refresh(
            s, user, now=rome(start + timedelta(days=27), 20))
        await s.commit()
    assert granted == streak.MILESTONE_DAYS * 2
    async with api_db() as s:
        assert await streak.paid_milestones(s, CHAT) == {
            start + timedelta(days=13), start + timedelta(days=27)}


async def test_a_rebuilt_streak_pays_its_milestone_again(api_db, registered):
    """Keyed on the DAY it was reached, not on "milestone number one". A learner who keeps
    a fortnight, lapses for a week, and keeps another fortnight has earned it twice — and
    under the owner's decision both are paid."""
    first = date(2026, 7, 1)
    await _fortnight(api_db, first)
    async with api_db() as s:
        user = await s.get(User, CHAT)
        await streak.refresh(s, user, now=rome(first + timedelta(days=13), 20))
        await s.commit()

    second = date(2026, 8, 1)
    await _fortnight(api_db, second)
    async with api_db() as s:
        user = await s.get(User, CHAT)
        _d, _f, granted = await streak.refresh(
            s, user, now=rome(second + timedelta(days=13), 20))
        await s.commit()
    assert granted == streak.MILESTONE_DAYS, "the second fortnight was refused as a repeat"
    async with api_db() as s:
        assert await streak.paid_milestones(s, CHAT) == {
            first + timedelta(days=13), second + timedelta(days=13)}


async def test_a_broken_streak_pays_nothing_for_the_days_it_had(api_db, registered):
    """Thirteen days, a gap, thirteen more. Twenty-six qualifying days and no fortnight."""
    start = date(2026, 7, 1)
    await _fortnight(api_db, start, days=13)
    await _fortnight(api_db, start + timedelta(days=15), days=13)
    async with api_db() as s:
        user = await s.get(User, CHAT)
        _d, _f, granted = await streak.refresh(
            s, user, now=rome(start + timedelta(days=27), 20))
        await s.commit()
    assert granted == 0


async def test_the_grant_extends_an_existing_pass_rather_than_shortening_it(api_db,
                                                                           registered):
    """Granting to somebody who already has Premium must add days to the end. Setting the
    expiry to "now plus three" would CUT SHORT a paying subscriber for keeping a streak."""
    start = date(2026, 7, 1)
    await _fortnight(api_db, start)
    now = rome(start + timedelta(days=13), 20)
    far = now + timedelta(days=200)
    async with api_db() as s:
        user = await s.get(User, CHAT)
        user.pass_expires_at = far
        await streak.refresh(s, user, now=now)
        await s.commit()
    async with api_db() as s:
        assert (await s.get(User, CHAT)).pass_expires_at == far + timedelta(
            days=streak.MILESTONE_DAYS)


async def test_an_expired_pass_is_not_extended_from_the_past(api_db, registered):
    """The other half of the same rule: someone whose pass lapsed in May must get three days
    from now, not three days from May — which would grant nothing at all."""
    start = date(2026, 7, 1)
    await _fortnight(api_db, start)
    now = rome(start + timedelta(days=13), 20)
    async with api_db() as s:
        user = await s.get(User, CHAT)
        user.pass_expires_at = now - timedelta(days=60)
        await streak.refresh(s, user, now=now)
        await s.commit()
    async with api_db() as s:
        assert (await s.get(User, CHAT)).pass_expires_at > now


async def test_the_payout_is_recorded_with_what_bought_it(api_db, registered):
    start = date(2026, 7, 1)
    await _fortnight(api_db, start)
    async with api_db() as s:
        user = await s.get(User, CHAT)
        await streak.refresh(s, user, now=rome(start + timedelta(days=13), 20))
        await s.commit()
    async with api_db() as s:
        from sqlalchemy import select
        row = (await s.scalars(select(Event).where(
            Event.chat_id == CHAT, Event.type == EV_STREAK_MILESTONE))).one()
    assert row.payload["day"] == (start + timedelta(days=13)).isoformat()
    assert row.payload["nth"] == 1
    assert row.payload["days"] == streak.MILESTONE_DAYS


# --- the write path -----------------------------------------------------------

async def test_the_day_is_written_once_even_under_a_double_submit(api_db, registered):
    """The primary key is the guarantee, not the existence check in front of it."""
    from sqlalchemy import func, select

    day = rome(date(2026, 7, 31), 12)
    await answer_many(api_db, day, GOAL)
    async with api_db() as s:
        # Same instant, same day, straight back into the writer.
        again = await streak.note_answer(s, CHAT, day + timedelta(seconds=(GOAL - 1) * 10))
        await s.commit()
        total = await s.scalar(select(func.count()).select_from(StreakDay)
                               .where(StreakDay.chat_id == CHAT))
    assert total == 1
    assert not again


async def test_a_day_written_between_the_check_and_the_insert_is_not_written_twice(
        api_db, registered, monkeypatch):
    """The race the primary key is actually for.

    Two answers arrive together. Both read the state before either has written: neither sees
    a row for today, and both see yesterday as the last day earned, so both clear the gap and
    both try to insert. Simulated by making one call read the state as it was BEFORE the
    other committed, which is precisely what a concurrent request sees.

    What is under test is the INSERT refusing — not the SELECT in front of it, which is only
    there to keep the common case cheap. Without ON CONFLICT this raises and the answer is
    lost; without the rowcount check the losing side reports a second earned day and the
    client celebrates twice.
    """
    from sqlalchemy import func, select

    yesterday = rome(date(2026, 7, 30), 12)
    today = rome(date(2026, 7, 31), 12)
    await answer_many(api_db, yesterday, GOAL, first_id=1)
    await answer_many(api_db, today, GOAL, first_id=100)

    async with api_db() as s:
        real_get = s.get

        async def unseeing_get(entity, ident, *a, **kw):
            if entity is StreakDay:
                return None                       # today's row is not visible to us yet
            return await real_get(entity, ident, *a, **kw)

        async def stale_last(_session, _chat_id):
            return yesterday                      # ...and neither is its qualifying instant

        s.get = unseeing_get
        monkeypatch.setattr(streak, "last_qualified_at", stale_last)
        raced = await streak.note_answer(s, CHAT, today + timedelta(minutes=1))
        s.get = real_get
        await s.commit()
        total = await s.scalar(select(func.count()).select_from(StreakDay)
                               .where(StreakDay.chat_id == CHAT))
    assert total == 2, "the race wrote a day twice"
    assert not raced, "the losing side of the race reported earning the day"


async def test_the_day_records_what_it_was_earned_with(api_db, registered):
    await answer_many(api_db, rome(date(2026, 7, 31), 12), GOAL + 5)
    async with api_db() as s:
        row = await s.get(StreakDay, (CHAT, "2026-07-31"))
    assert row.questions == GOAL, \
        "the day should record the count that earned it, not whatever came later"


@pytest.mark.parametrize("n", [0, 1, GOAL - 1])
async def test_nothing_is_written_before_the_goal(api_db, registered, n):
    from sqlalchemy import func, select

    await answer_many(api_db, rome(date(2026, 7, 31), 12), n)
    async with api_db() as s:
        assert await s.scalar(select(func.count()).select_from(StreakDay)) == 0


# --- the whole way to the client ----------------------------------------------

def _headers(chat_id: int = CHAT) -> dict[str, str]:
    import json
    import time as clock

    from api.services.telegram_auth import sign
    from shared.config import settings

    token = "8918020834:AAEtest-token-not-real-only-for-tests"
    settings.bot_token_prod = token
    settings.env = "prod"
    return {"X-Telegram-Init-Data": sign(
        {"user": json.dumps({"id": chat_id}, separators=(",", ":")),
         "auth_date": str(int(clock.time()))}, token)}


async def test_answering_through_the_api_moves_the_goal(client, registered):
    """End to end, through the endpoint a learner actually uses.

    The service is not the boundary: `AnswerOut` strips any field it does not declare, and a
    key added to the service payload alone reaches nobody while every service-level test
    keeps passing. That failure looks exactly like success, so it is asserted here.
    """
    r = await client.post(f"/users/{CHAT}/answers", json={"question_id": 1, "answer": True})
    assert r.status_code == 200
    assert r.json()["streak_earned_today"] is False, \
        "one answer is not a day — and the field must survive the response model"

    body = (await client.get("/webapp/profile", headers=_headers())).json()
    assert body["streak_today"] == 1
    assert body["streak_days"] == 0


async def test_the_bank_is_too_small_here_to_finish_a_day_by_accident(client, registered):
    """The fixture holds four questions, so the goal cannot be reached through the API in
    this suite. Stated rather than left implicit: a later fixture with ten questions would
    change what the test above proves, silently."""
    from api.services.streak import GOAL
    assert GOAL > 4


async def test_the_answer_that_earns_the_day_says_so_to_the_client(client, registered,
                                                                   monkeypatch):
    """The True case, through the endpoint. The False case alone is not enough: hardcoding
    the field to False passes every other test here, and the client would then never
    celebrate anything.

    Two things are stood aside to get there, both with their own tests elsewhere. The goal is
    lowered because this suite's bank holds four questions; pacing is held open because three
    HTTP calls in a row are milliseconds apart and would all be uncredited, which is pacing
    working correctly and is not what is under test here.
    """
    from api.services import answers as answers_service
    from api.services import pacing

    monkeypatch.setattr(streak, "GOAL", 3)

    async def always_credited(_session, _chat_id, _now=None):
        return True

    monkeypatch.setattr(pacing, "check", always_credited)
    monkeypatch.setattr(answers_service.pacing, "check", always_credited)

    flags = []
    for qid in (1, 2, 3):
        r = await client.post(f"/users/{CHAT}/answers", json={"question_id": qid, "answer": True})
        assert r.status_code == 200
        flags.append(r.json()["streak_earned_today"])

    assert flags == [False, False, True], \
        f"the goal should be announced once, on the answer that completed it — got {flags}"

    body = (await client.get("/webapp/profile", headers=_headers())).json()
    assert body["streak_days"] == 1
