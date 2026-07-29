"""Request and response contracts.

Both the bot and the Mini App consume these, so the shape is the API's public
surface. Locked content is absent rather than null-with-a-flag wherever possible,
and every gated field is paired with a `*_state` telling the client which of
shown / locked / unavailable / off applies — so a client can render a paywall
without ever having to decide entitlement itself.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from shared.constants import TIERS, UI_LANGUAGES


class UserIn(BaseModel):
    chat_id: int
    lang: str | None = None


class UserSettingsIn(BaseModel):
    lang: str | None = Field(default=None, description=f"one of {UI_LANGUAGES}")
    translations_on: bool | None = None
    onboarded: bool | None = None


class UserOut(BaseModel):
    chat_id: int
    lang: str
    translations_on: bool
    pass_expires_at: datetime | None
    has_pass: bool
    free_explanations_left: int
    onboarded_at: datetime | None
    created_at: datetime


class TranslationOut(BaseModel):
    lang: str
    stem: str | None = None
    statement: str


class QuestionOut(BaseModel):
    id: int
    quesito_id: int
    topic_id: int
    statement_it: str
    stem_it: str | None = None
    image: str | None = None
    translation_state: str
    translation: TranslationOut | None = None


class AnswerIn(BaseModel):
    question_id: int
    answer: bool = Field(description="true = VERO, false = FALSO")


class AnswerOut(BaseModel):
    question_id: int
    given: bool
    correct: bool
    correct_answer: bool
    box: int
    due_at: datetime
    explanation_state: str
    explanation: str | None = None
    free_explanations_left: int


class TopicStat(BaseModel):
    topic_id: int
    topic: str
    questions_seen: int
    answers_given: int
    wrong: int
    error_rate: float


class StatsOut(BaseModel):
    questions_seen: int
    questions_total: int
    answers_given: int
    wrong: int
    error_rate: float
    boxes: dict[str, int]
    by_topic: list[TopicStat]


class TopicOut(BaseModel):
    id: int
    name: str
    questions: int


class ReportIn(BaseModel):
    question_id: int


class GrantIn(BaseModel):
    """Admin override — the 11pm fix when a webhook is missed (plan §12)."""

    chat_id: int
    tier: str = Field(description=f"one of {TIERS}")
    reason: str | None = None
