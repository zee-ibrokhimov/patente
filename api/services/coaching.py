"""AI study advice, on top of the numbers we computed ourselves.

The breakdown screen already says WHERE a learner is losing marks. This says what to do
about it, in their own language, using their own recent mistakes.

IT NEVER GATES THE SCREEN. If the model is slow, refuses, or is not configured, the caller
renders the deterministic breakdown it already has. A study screen that shows a spinner
because an API is having a bad afternoon is worse than one that never offered the button.

WHAT IS SENT, AND WHAT IS NOT
No name, no Telegram id, no chat id, no language history, nothing that identifies anybody.
What goes is the topic names and up to WRONG_SAMPLE of the learner's own recent wrong
answers, drawn from their worst families. That sample is the entire difference between
specific advice and a horoscope, and it is also the only reason this costs anything.

TWO RULES THE MODEL IS HELD TO, BOTH ENFORCED HERE AND NOT ONLY ASKED FOR
  · NO NUMBERS IN THE PROSE. Every figure on that screen is computed by us. A model that
    writes "your error rate is 31%" beside our computed 24% breaks the one promise the
    product makes about its own numbers. Stripped after the fact, because asking is not
    enforcing.
  · IT MAY NOT TEACH TRAFFIC LAW. It says where and how to study, never what a rule IS.
    Rules are the explanations feature's job and those are grounded in the statute; an
    ungrounded model inventing a speed limit is exactly the failure that system exists to
    prevent.

THE COOLDOWN IS CHECKED FIRST, BEFORE ANYTHING ELSE
Language is a detail underneath it. Checked the other way round, four taps in Settings buy
four analyses inside one window, and one account can drive hundreds of calls an hour on a
EUR 2.99 subscription. There is also a hard monthly ceiling as a backstop, because a
cooldown is a rate and a rate with no cap is still unbounded over time.

THE RETRY LADDER IS COPIED FROM translations.py ON PURPOSE
That module records a bug worth not repeating: a single fallback to NO parameters meant
`reasoning_effort` was silently dropped on every call, because the model rejects
`temperature=0`. The constant was set, the tests passed, and production never once ran what
it was configured to run — at 5-10x the cost. Degrade one parameter at a time, and log the
token count so a silent revert is visible in the data.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.models import Analysis, Event, Question, Topic
from api.services.entitlement import Entitlement
from api.services.explanations import is_fatal, openai_client
from shared.config import settings
from shared.constants import EV_ANSWER_GIVEN

log = logging.getLogger(__name__)

# Same effort setting, and the same reason, as translations: most of the wall-clock a
# learner sits through is reasoning, and this is a summary of numbers we already computed
# rather than a piece of reasoning in its own right.
REASONING_EFFORT = "low"

# Two days. The learner's answers do not change fast enough for a fresh reading to say
# anything new, and this is the only thing setting the slope of the one AI cost in this
# product that scales with users.
COOLDOWN = timedelta(hours=48)

# A backstop under the cooldown. A rate with no ceiling is still unbounded over a month.
MONTHLY_CAP = 20

# How many of the learner's own wrong answers go into the prompt. Enough to be specific,
# few enough that the prompt does not become the cost.
WRONG_SAMPLE = 30

# What a free learner must have done before the single taster is offered. Both halves
# matter: the answers make the advice worth reading, and the account age is what stops a
# fresh account being made to harvest one.
TASTER_MIN_ANSWERS = 100
TASTER_MIN_AGE = timedelta(days=7)

SYSTEM_PROMPT = """\
You are a study coach for the Italian driving-licence theory exam (patente B).

You are given one learner's weakest topics and a sample of questions they actually got
wrong. Write advice about HOW AND WHERE TO STUDY.

HARD RULES:

1. NEVER STATE A TRAFFIC RULE. Do not say what a sign means, what a limit is, what is
   required or forbidden. If a learner needs that, the app has explanations written against
   the statute. You are not that feature and you will be wrong.

2. NO NUMBERS AT ALL. No percentages, no counts, no scores, no "you got N wrong". Every
   figure the learner sees is computed by the app and shown beside your text. A number from
   you that disagrees with it destroys their trust in both.

3. BE SPECIFIC TO THE MISTAKES YOU WERE GIVEN. Name the actual confusions visible in them.
   Generic advice — "practise more", "read carefully" — is worse than nothing, because the
   learner paid attention to read it.

4. Write in the language you are asked for, plainly, as one person to another. No
   headings, no lists inside the fields, no encouragement padding.

Answer as JSON:
{
  "summary": "two sentences: what the pattern in these mistakes actually is",
  "focus": [
    {"area": "short label", "action": "one concrete thing to do next"},
    ... exactly three ...
  ],
  "habit": "one sentence: a reading or answering habit that would prevent this class of
            mistake",
  "next_up": "one short sentence naming what to practise in the next sitting"
}
"""

# Any digit at all. The rule is "no numbers", and enforcing it with a regex is the
# difference between a rule and a request — models comply with this one most of the time,
# which is exactly the failure mode that goes unnoticed.
_DIGITS = re.compile(r"\d")


def _strip_numbers(text: str) -> str:
    """Remove digits from model prose.

    Not a rejection: one stray figure should not cost the learner their advice. The
    sentence survives, the number does not, and the numbers on the screen stay the ones the
    app computed.
    """
    return _DIGITS.sub("", text or "").replace("  ", " ").strip()


def _clean(parsed: dict) -> dict | None:
    """Shape the model's answer, or None if it did not answer usefully."""
    summary = _strip_numbers(str(parsed.get("summary", "")))
    habit = _strip_numbers(str(parsed.get("habit", "")))
    next_up = _strip_numbers(str(parsed.get("next_up", "")))

    focus = []
    for item in (parsed.get("focus") or [])[:3]:
        if not isinstance(item, dict):
            continue
        area = _strip_numbers(str(item.get("area", "")))
        action = _strip_numbers(str(item.get("action", "")))
        if area and action:
            focus.append({"area": area, "action": action})

    if not summary or not focus:
        return None
    return {"summary": summary, "focus": focus, "habit": habit, "next_up": next_up}


async def latest(session: AsyncSession, chat_id: int) -> Analysis | None:
    return await session.scalar(
        select(Analysis).where(Analysis.chat_id == chat_id)
        .order_by(Analysis.created_at.desc()).limit(1))


async def may_generate(
    session: AsyncSession, user, entitlement: Entitlement, now: datetime | None = None
) -> tuple[bool, str]:
    """Whether a fresh analysis may be made, and why not when it may not.

    THE COOLDOWN IS CHECKED BEFORE THE ENTITLEMENT AND BEFORE THE LANGUAGE. Checked after
    either, a learner who changes a setting gets a new one, and the cooldown stops being a
    cooldown.
    """
    now = now or datetime.now(timezone.utc)

    last = await latest(session, user.chat_id)
    if last is not None:
        made = last.created_at
        if made.tzinfo is None:
            made = made.replace(tzinfo=timezone.utc)
        if now - made < COOLDOWN:
            return False, "cooldown"

    since = now - timedelta(days=30)
    recent = await session.scalar(
        select(func.count(Analysis.id))
        .where(Analysis.chat_id == user.chat_id, Analysis.created_at >= since)) or 0
    if recent >= MONTHLY_CAP:
        return False, "monthly_cap"

    # `.premium`, not `.can_explain`. The latter is true for a free learner who still has
    # taster EXPLANATIONS left, which is a different allowance entirely — gating on it hands
    # every free account an analysis and makes the taster below unreachable.
    if entitlement.premium:
        return True, "premium"

    # The single free taster. Both conditions matter: the answers are what make the advice
    # worth reading at all, and the account age is what stops a fresh account being created
    # to harvest one.
    ever = await session.scalar(
        select(func.count(Analysis.id)).where(Analysis.chat_id == user.chat_id)) or 0
    if ever:
        return False, "locked"

    answers = await session.scalar(
        select(func.count(Event.id))
        .where(Event.chat_id == user.chat_id, Event.type == EV_ANSWER_GIVEN)) or 0
    if answers < TASTER_MIN_ANSWERS:
        return False, "too_early"

    created = getattr(user, "created_at", None)
    if created is not None:
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if now - created < TASTER_MIN_AGE:
            return False, "too_early"

    return True, "taster"


async def _sample_mistakes(session: AsyncSession, chat_id: int, limit: int) -> list[dict]:
    """The learner's recent wrong answers, with the question text and its topic.

    The entire difference between specific advice and a horoscope. Nothing identifying goes
    with them — see the module docstring.
    """
    rows = list(await session.scalars(
        select(Event.payload)
        .where(Event.chat_id == chat_id, Event.type == EV_ANSWER_GIVEN)
        .order_by(Event.created_at.desc()).limit(limit * 6)))

    wrong_ids: list[int] = []
    for payload in rows:
        if not isinstance(payload, dict) or payload.get("correct") is not False:
            continue
        qid = payload.get("question_id")
        if qid is not None and qid not in wrong_ids:
            wrong_ids.append(qid)
        if len(wrong_ids) >= limit:
            break
    if not wrong_ids:
        return []

    questions = (await session.execute(
        select(Question.id, Question.statement_it, Topic.name)
        .join(Topic, Topic.id == Question.topic_id)
        .where(Question.id.in_(wrong_ids)))).all()
    return [{"topic": name, "statement": statement}
            for _qid, statement, name in questions]


async def generate(
    session: AsyncSession, user, report: dict, now: datetime | None = None
) -> dict | None:
    """Ask the model. Returns None on any failure — the caller already has a screen.

    Every path that returns None is a path where the learner sees the deterministic
    breakdown and no error: an AI layer that can break the page it sits on is not worth the
    page.
    """
    now = now or datetime.now(timezone.utc)
    if not settings.openai_api_key:
        return None

    mistakes = await _sample_mistakes(session, user.chat_id, WRONG_SAMPLE)
    if not mistakes:
        return None

    weakest = [f["family"] for f in report.get("families", [])
               if f.get("predicted_mistakes") is not None][:5]

    payload = {
        "language": user.lang,
        "weakest_areas": weakest,
        "recent_mistakes": mistakes,
    }

    client = openai_client()
    kwargs = dict(
        model=settings.translate_model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        response_format={"type": "json_object"},
    )
    # Copied from translations.py, including the reason. A single fallback to no parameters
    # silently dropped `reasoning_effort` on every call there, because the model rejects
    # `temperature=0` — the constant was set, the tests passed, and production never once
    # ran what it was configured to run.
    attempts = (
        dict(temperature=0, reasoning_effort=REASONING_EFFORT),
        dict(reasoning_effort=REASONING_EFFORT),
        dict(temperature=0),
        dict(),
    )
    try:
        response = None
        last_exc: Exception | None = None
        for extra in attempts:
            try:
                response = await client.chat.completions.create(**extra, **kwargs)
                break
            except Exception as exc:  # noqa: BLE001
                text = str(exc)
                if "temperature" not in text and "reasoning_effort" not in text:
                    raise
                last_exc = exc
        if response is None:
            raise last_exc or RuntimeError("no usable parameter combination")
        parsed = json.loads(response.choices[0].message.content)
    except Exception as exc:  # noqa: BLE001
        log.error("analysis failed for a learner: %s %s%s",
                  type(exc).__name__, exc,
                  " (fatal — the feature is down)" if is_fatal(exc) else "")
        return None

    body = _clean(parsed)
    if body is None:
        log.warning("analysis came back unusable")
        return None

    usage = getattr(response, "usage", None)
    row = Analysis(
        chat_id=user.chat_id, lang=user.lang, body=body,
        tokens_in=getattr(usage, "prompt_tokens", 0) or 0,
        tokens_out=getattr(usage, "completion_tokens", 0) or 0,
        created_at=now,
    )
    session.add(row)
    await session.flush()
    # Logged on every generation so a silent revert to an expensive configuration shows up
    # in the data rather than in the invoice.
    log.info("analysis generated: in=%s out=%s", row.tokens_in, row.tokens_out)
    return body
