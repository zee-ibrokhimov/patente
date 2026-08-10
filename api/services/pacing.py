"""How fast a learner may answer, and how many times a day.

`POST /webapp/answers` accepts any question id, any number of times, with no sitting, no
pacing rule and no limit. That was harmless while nothing depended on the count. It stops
being harmless the moment a streak, a league position or a free week of Premium is derived
from it: every threshold in those designs is then a count of HTTP requests rather than a
measure of study, and a hundred of them takes about thirty seconds to fake.

MEASURED, NOT GUESSED
The gap below comes from the real event log: the median interval between two answers by a
genuine learner is 7.4 seconds, and exactly one genuine gap in about 129 falls under 2.5
seconds. A scripted run against the same database put 64 of 78 gaps under 2 seconds. So a
one-second floor is invisible to a person reading a question and fatal to a loop.

TWO RULES, AND ONLY ONE OF THEM REFUSES ANYTHING
The first version refused an answer that came too fast, and sixteen existing tests failed
immediately — every one of them a sitting answered back to back. That is not a test problem.
It is the false-positive cost, measured: a candidate who already knows the sign can tap in
well under a second, and doing it twice in a row is not evidence of anything. Refusing a
real answer mid-exam is worse than letting a farmer through.

So the floor no longer refuses. It decides whether an answer is CREDITED — whether it may
count toward a streak, a league point or a free week of Premium. A fast answer is still
recorded, still moves the Leitner schedule, still appears in the learner's own statistics,
because their study is real either way. It simply earns nothing. A script gets exactly what
it deserves: rows that do not pay.

The daily cap still refuses, because that one has no false positives worth the name — the
heaviest genuine day in the log is 83 answers and the cap is 500.

WHY IT IS ENFORCED IN THE DATABASE AND NOT IN MEMORY
A process-local counter resets on every deploy and is per-worker, so the real limit would be
"the cap, times the number of workers, plus once more each time you ship". The event log
already has the index this needs (`ix_event_chat_time`), the volume is one indexed COUNT per
answer, and it survives restarts because it is the same record everything else is derived
from.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.models import Event
from shared.constants import EV_ANSWER_GIVEN

# One second. See the module docstring: a floor that no reader notices and no loop survives.
MIN_GAP = timedelta(seconds=1)

# A day's answers. The heaviest genuine day in the log is 83 answers over 68 distinct
# questions; 500 is six times that. Nobody studying reaches it, and a script reaches it in
# under a minute — which is the point: the cap is not there to pace a learner, it is there
# to bound what a runaway can write.
DAILY_CAP = 500


class TooFast(Exception):
    """Past the day's cap.

    Carries `retry_after` seconds so the route can say so rather than returning a bare
    refusal that a client cannot act on.
    """

    def __init__(self, message: str, retry_after: int = 1) -> None:
        super().__init__(message)
        self.retry_after = retry_after


async def check(session: AsyncSession, chat_id: int, now: datetime | None = None) -> bool:
    """Refuse this answer if it is one too many today; otherwise say whether it is CREDITED.

    Returns True when the answer may count toward a streak, a league point or a reward, and
    False when it arrived too fast to be evidence of anything. Raises TooFast only for the
    daily cap, which is the one rule here worth refusing over.

    Called before the answer is written, so the verdict can be stamped on the event itself —
    events cannot be backfilled, and a credit rule that has to be re-derived later from
    timestamps is a rule that changes retroactively every time it is edited.
    """
    now = now or datetime.now(timezone.utc)

    since = now - timedelta(days=1)
    today = await session.scalar(
        select(func.count(Event.id))
        .where(Event.chat_id == chat_id, Event.type == EV_ANSWER_GIVEN,
               Event.created_at >= since)
    ) or 0
    if today >= DAILY_CAP:
        # A rolling day rather than midnight-to-midnight: the learner is never told to come
        # back "tomorrow" at 00:01 having been cut off at 23:59, and there is no timezone to
        # get wrong.
        raise TooFast("daily answer limit reached", retry_after=3600)

    last = await session.scalar(
        select(func.max(Event.created_at))
        .where(Event.chat_id == chat_id, Event.type == EV_ANSWER_GIVEN)
    )
    if last is None:
        return True
    # SQLite hands back a naive datetime; everything here is UTC by construction.
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return now - last >= MIN_GAP
