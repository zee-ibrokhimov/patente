"""Practising one subject instead of the whole bank.

WHY THIS EXISTS, AND WHY IT IS PRACTICE ONLY

The error-analysis screen already tells a learner where they are losing marks — it ranks the
seven families by predicted mistakes per exam and says "this one is costing you 3.2". Until
now it then offered them nothing to do about it. This is the other half of that screen.

The exam does NOT get this, and that is a rule rather than an omission: `selection.exam_paper`
draws uniformly because that is what an exam IS, and a simulator quietly weighted toward
chosen topics reports a score that means nothing. Choosing what to study is teaching;
choosing what to be tested on is cheating yourself.

TWO LEVELS, BECAUSE LEARNERS ARRIVE FROM TWO DIRECTIONS

  · seven FAMILIES, in plain language, sized to be read on a phone in ten seconds. This is
    what the analysis screen already ranks, so the diagnosis and the cure use one vocabulary.
  · the twenty-five MINISTERIAL TOPICS underneath them, under their official names, because
    every Italian study book is organised by exactly those chapters and somebody working
    through one wants "Segnali di pericolo" and not "road signs".

NO TOPIC IS TOO SMALL TO PRACTISE. Measured against the real bank before this was built: the
smallest ministerial topic holds 103 questions and the largest 662, so even the narrowest
choice is three full sittings before anything can repeat. That is what made the two-level
design safe; had the tail been twenty-question topics, only families would have been offered.

THE SCOPE IS A STRING, AND IT IS VALIDATED HERE

`None` is the whole bank and is what every existing sitting has. A family key is one of
TOPIC_FAMILIES. `topic:<id>` is a single ministerial topic. Parsed in one place, so a scope
that reaches the draw is one that already exists — an unknown one is refused rather than
silently widening the sitting back to everything, which would look like the feature failing
to work rather than like an error.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.models import Question, Topic
from shared.constants import TOPIC_FAMILIES

TOPIC_PREFIX = "topic:"


class UnknownScope(ValueError):
    """A scope that names no family and no topic."""


def topic_ids(scope: str | None) -> set[int] | None:
    """The topics a scope covers. None means "the whole bank", which is not the same as
    an empty set — one is no filter at all, the other filters everything out."""
    if scope is None or scope == "":
        return None
    if scope in TOPIC_FAMILIES:
        return set(TOPIC_FAMILIES[scope])
    if scope.startswith(TOPIC_PREFIX):
        raw = scope[len(TOPIC_PREFIX):]
        if raw.isdigit():
            return {int(raw)}
    raise UnknownScope(f"unknown scope {scope!r}")


def is_valid(scope: str | None) -> bool:
    try:
        topic_ids(scope)
    except UnknownScope:
        return False
    return True


async def catalogue(session: AsyncSession, chat_id: int) -> list[dict]:
    """The seven families, each with its topics, ranked by the marks they are costing.

    The ranking, the error rate and the coverage all come from `analysis.families` rather
    than being recomputed here. Two screens quoting the same quantity from two different
    calculations is two screens that will eventually disagree, and the learner cannot tell
    which one to believe.
    """
    from api.services import analysis

    ranked = await analysis.families(session, chat_id)
    names = dict((await session.execute(select(Topic.id, Topic.name))).all())
    sizes = dict((await session.execute(
        select(Question.topic_id, func.count(Question.id)).group_by(Question.topic_id)
    )).all())

    out = []
    for row in ranked:
        family = row["family"]
        topics = [
            {
                "scope": f"{TOPIC_PREFIX}{tid}",
                "topic_id": tid,
                # The ministerial name, verbatim and untranslated. It is the string printed
                # in every study book and on the exam paper itself; rendering a helpful
                # translation would break the one job this list has, which is matching what
                # the learner is holding.
                "name": names.get(tid, str(tid)),
                "questions": sizes.get(tid, 0),
            }
            for tid in TOPIC_FAMILIES[family]
        ]
        out.append({
            "scope": family,
            "family": family,
            "questions": row["questions_in_bank"],
            "per_exam": row["per_exam"],
            "error_rate": row["error_rate"],
            "coverage": row["coverage"],
            "predicted_mistakes": row["predicted_mistakes"],
            # Sorted biggest-first inside the family too, so the topic most likely to be
            # worth practising is the one under the learner's thumb.
            "topics": sorted(topics, key=lambda t: -t["questions"]),
        })
    return out
