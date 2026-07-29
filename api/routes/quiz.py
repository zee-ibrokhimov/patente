from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_session, get_user
from api.models import Question, Report, Topic, User
from api.schemas import AnswerIn, AnswerOut, QuestionOut, ReportIn, StatsOut, TopicOut
from api.services import events, stats
from api.services.answers import record_answer
from api.services.content import question_payload
from api.services.entitlement import evaluate
from api.services.selection import next_question
from shared.constants import EV_QUESTION_SERVED, EV_REPORT_SUBMITTED

router = APIRouter(tags=["quiz"])


@router.get("/topics", response_model=list[TopicOut])
async def list_topics(session: AsyncSession = Depends(get_session)):
    rows = (
        await session.execute(
            select(Topic.id, Topic.name, func.count(Question.id))
            .join(Question, Question.topic_id == Topic.id)
            .group_by(Topic.id, Topic.name)
            .order_by(Topic.name)
        )
    ).all()
    return [TopicOut(id=i, name=n, questions=c) for i, n, c in rows]


@router.get("/users/{chat_id}/next-question", response_model=QuestionOut)
async def serve_next(
    topic_id: int | None = Query(default=None),
    exclude_id: int | None = Query(default=None, description="question just answered"),
    user: User = Depends(get_user),
    session: AsyncSession = Depends(get_session),
):
    entitlement = evaluate(user)
    question = await next_question(
        session, user, entitlement, topic_id=topic_id, exclude_id=exclude_id
    )
    if question is None:
        raise HTTPException(404, "no question available for that filter")

    payload = await question_payload(session, question, user, entitlement)
    await events.record(
        session,
        EV_QUESTION_SERVED,
        chat_id=user.chat_id,
        question_id=question.id,
        topic_id=question.topic_id,
        translation_state=payload["translation_state"],
    )
    return QuestionOut(**payload)


@router.post("/users/{chat_id}/answers", response_model=AnswerOut)
async def submit_answer(
    body: AnswerIn,
    user: User = Depends(get_user),
    session: AsyncSession = Depends(get_session),
):
    question = await session.get(Question, body.question_id)
    if question is None:
        raise HTTPException(404, "unknown question")

    result = await record_answer(
        session, user, question, body.answer, evaluate(user)
    )
    return AnswerOut(**result)


@router.get("/users/{chat_id}/stats", response_model=StatsOut)
async def read_stats(
    user: User = Depends(get_user), session: AsyncSession = Depends(get_session)
):
    entitlement = evaluate(user)
    if not entitlement.can_see_stats:
        raise HTTPException(402, "stats require an active pass")
    return StatsOut(**await stats.user_stats(session, user.chat_id))


@router.post("/users/{chat_id}/reports", status_code=201)
async def submit_report(
    body: ReportIn,
    user: User = Depends(get_user),
    session: AsyncSession = Depends(get_session),
):
    """"This explanation is wrong."

    Volume per 1,000 questions served is a headline quality metric, and these are
    the first place to look when estimating the correction rate (plan §9).
    """
    question = await session.get(Question, body.question_id)
    if question is None:
        raise HTTPException(404, "unknown question")

    session.add(
        Report(
            chat_id=user.chat_id,
            question_id=question.id,
            cluster_id=question.cluster_id,
            lang=user.lang,
        )
    )
    await events.record(
        session,
        EV_REPORT_SUBMITTED,
        chat_id=user.chat_id,
        question_id=question.id,
        cluster_id=question.cluster_id,
        lang=user.lang,
    )
    return {"ok": True}
