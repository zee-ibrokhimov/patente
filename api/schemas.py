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

from shared.constants import (
    QUIZ_MODES,
    REPEAT_SMART,
    REPEAT_SOURCES,
    TIERS,
    UI_LANGUAGES,
    VOCAB_DIRECTIONS,
)


class UserIn(BaseModel):
    chat_id: int
    lang: str | None = None
    # The /start deep-link payload, so acquisition can be attributed. Recorded once, at
    # first contact — see api/services/users.py.
    source: str | None = Field(default=None, max_length=64)


class UserSettingsIn(BaseModel):
    lang: str | None = Field(default=None, description=f"one of {UI_LANGUAGES}")
    translations_on: bool | None = None
    onboarded: bool | None = None
    # Hide me from the weekly league. The switch that makes showing real first names to
    # other learners defensible: one tap, immediate, and retroactive.
    leaderboard_opt_out: bool | None = None


class UserOut(BaseModel):
    chat_id: int
    lang: str
    translations_on: bool
    pass_expires_at: datetime | None
    has_pass: bool
    # A trial is a pass with an expiry and no purchase behind it, so the client needs
    # both flags to tell a paying subscriber from someone still trialling.
    purchased: bool
    # A sitting this user walked away from and can still return to.
    #
    # The client only knew about an open sitting if it had watched the user leave one in
    # the SAME page load. Close the Mini App mid-exam — switch apps, take a call, let the
    # phone lock — and it was gone: still open on the server with its twenty minutes
    # running down, and no way back to it. Losing a timed exam by answering a phone call
    # is the kind of thing that makes someone delete an app.
    open_session_id: int | None = None

    # The question every paid surface actually asks. `has_pass` alone is NOT it: the
    # server enforces `entitlement.premium`, which is a pass OR channel membership OR
    # staff — so a learner who is Premium through the channel was shown a paywall on
    # every screen while the API served them everything behind it.
    premium: bool = False
    premium_via: str = Field(
        default="none",
        description="pass | channel | staff | none — why they have it, for the UI to explain",
    )

    # Where to send someone who wants to pay. The Mini App cannot know the bot it was
    # opened from — Telegram does not tell it — so without this the buy button has
    # nowhere to go, which is exactly what it had.
    bot_username: str = ""
    support_contact: str = ""
    trialing: bool = Field(
        default=False,
        description="a Tribute trial with a card attached, as opposed to a granted pass",
    )
    # Hidden from the weekly league. The client renders the Settings switch from this, and
    # a switch that cannot read its own state would show everyone as visible.
    leaderboard_opt_out: bool = False
    # How many terms the glossary actually holds, counted rather than remembered.
    #
    # The Vocabulary screen showed "1090 exam words" directly above "0 of 1104 learned" —
    # two counts of one table disagreeing in adjacent lines, because the headline was a
    # literal typed into eight locale strings while the progress line interpolated a real
    # query. The seed grew; the prose did not. A number describing the content has to be
    # read from the content, or it is only true on the day someone types it.
    vocab_terms: int = 0
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
    image_file_id: str | None = Field(
        default=None, description="cached Telegram file_id; re-send by id, do not re-upload"
    )
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
    # Which language the explanation is in. Practice delivers it WITH the verdict,
    # so this schema needs it as much as ExplanationOut does — an Uzbek learner is
    # served Russian and nothing said so.
    explanation_lang: str | None = None
    explanation_state: str
    explanation: str | None = None
    free_explanations_left: int


class GrantPassIn(BaseModel):
    """An admin grant. `reason` is free text and goes on the event, so that months later
    it is possible to tell a comped tester apart from a missed webhook."""

    days: int = Field(gt=0, le=3650)
    reason: str = Field(default="", max_length=200)


class QuestionTranslationOut(BaseModel):
    """The response to an explicit translation request.

    `translation` is present only when `translation_state` is "shown". "available" never
    comes back from here — by the time this has answered, the translation either exists or
    could not be produced. Named apart from `TranslationOut`, which is the body nested
    inside a question payload.
    """

    question_id: int
    translation_state: str
    translation: TranslationOut | None = None


class ExplanationOut(BaseModel):
    """`explanation` is present only when `explanation_state` is "shown".

    The other states are "locked" (pay for it), and "unavailable" — which covers the
    model declining, the call failing, and a draft withheld by a quality gate, because
    a user has no use for the difference between those three.
    """

    question_id: int
    # Which language the text is actually in. An Uzbek learner is served RUSSIAN — there
    # is a deliberate fallback, because a bad explanation is the only thing on screen and
    # is the thing being sold, so Uzbek ships as translations first. But nothing told
    # them: they paid for explanations and silently got a language they did not choose.
    # Saying so is the difference between a known limitation and a broken product.
    explanation_lang: str | None = None
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


# --- quiz sessions ----------------------------------------------------------

class SessionOut(BaseModel):
    """A sitting, plus its whole paper.

    The paper ships with the session on purpose. It is frozen at creation, and
    `QuestionOut` carries no answer key — so there is nothing to leak, and it removes
    thirty blocking round trips from a screen that has a clock running on it.

    `server_now` is not decoration. The client renders a countdown from
    `expires_at - (Date.now() + skew)` where `skew = server_now - Date.now()`, measured
    once. Without it the countdown is the device clock, which is wrong on a surprising
    number of phones and trivially editable on all of them. The server enforces the
    deadline regardless; this only makes the display honest.
    """

    id: int
    mode: str
    state: str
    started_at: datetime
    expires_at: datetime | None
    server_now: datetime
    question_count: int
    max_errors: int | None
    answered: int
    questions: list[QuestionOut]

    # Which ordinals already have an answer. ORDINALS ONLY — never `given` or `correct`.
    #
    # The client keeps no state across a reopen, so without this a resumed exam painted an
    # empty answer sheet: right question, right clock, thirty blank circles. Returning the
    # answers themselves would instead breach "an exam reveals nothing until it is over",
    # which ExamAnswerOut exists to defend. Knowing you answered question 7 is not knowing
    # whether you got it right.
    answered_ordinals: list[int] = []


class ExamAnswerOut(BaseModel):
    """What an exam answer returns: progress, and nothing else.

    No `correct`, no `correct_answer`, no `box`, no explanation state. The absence is
    the feature — there is a test asserting these keys are missing, because the natural
    way to break this is to add a field to the practice response and have it appear here
    by accident.
    """

    session_id: int
    ordinal: int
    answered: int
    remaining: int


class PracticeAnswerOut(AnswerOut):
    """Practice reveals everything an ordinary answer does, plus where you are."""

    session_id: int
    ordinal: int
    answered: int
    remaining: int


class SessionItemOut(BaseModel):
    ordinal: int
    question_id: int
    given: bool | None
    correct: bool | None

    # The question itself, so a candidate can see WHAT they got wrong rather than only
    # how many. Free content: the whole bank is in the free tier already.
    statement: str = ""
    stem: str | None = None
    answer: bool | None = None
    image: str | None = None
    # Present only for a user entitled to translations who has them switched on.
    translation: str | None = None


class SessionResultsOut(BaseModel):
    session_id: int
    mode: str
    state: str
    started_at: datetime
    finished_at: datetime | None
    question_count: int
    answered: int
    wrong: int
    max_errors: int | None
    passed: bool | None
    items: list[SessionItemOut]


class StartSessionIn(BaseModel):
    mode: str = Field(description=f"one of {QUIZ_MODES}")

    # What a PRACTICE sitting draws from. Ignored for an exam, which is always a uniform
    # random draw — a simulator built from your own mistakes reports a score that means
    # nothing. Defaults to the spaced-repetition order, so existing clients are unaffected.
    source: str = Field(default=REPEAT_SMART, description=f"one of {REPEAT_SOURCES}")


class SessionAnswerIn(BaseModel):
    ordinal: int = Field(ge=1)
    answer: bool = Field(description="true = VERO, false = FALSO")


# --- leaderboard ------------------------------------------------------------

class LeaderboardEntry(BaseModel):
    """One row. Deliberately the smallest thing that can be rendered.

    NO chat_id and no username. This is the only response in the product that carries one
    user's data to another, and anything here that could be used to FIND a person rather
    than merely rank them does not belong — a first name and a score cannot be looked up.
    """

    rank: int
    name: str | None = None
    score: int
    is_me: bool = False


class LeaderboardMeOut(BaseModel):
    """Where the caller stands. `rank` is null when they have not scored this week."""

    rank: int | None = None
    score: int = 0
    opted_out: bool = False


class LeaderboardOut(BaseModel):
    week_start: datetime
    # How many learners are ranked in total, so the client can tell a real competition from
    # three people and say so instead of rendering a podium nobody can move on.
    ranked: int
    entries: list[LeaderboardEntry]
    me: LeaderboardMeOut


# --- profile ----------------------------------------------------------------

class ExamHistoryOut(BaseModel):
    id: int
    finished_at: datetime | None
    wrong: int
    answered: int
    question_count: int
    passed: bool | None
    state: str


class ExamSummaryOut(BaseModel):
    taken: int
    passed: int
    avg_errors: float | None
    recent: list[ExamHistoryOut]


class ProfileOut(BaseModel):
    """`readiness` is null below `readiness_min_sample` answers rather than a flattering
    guess — see the module docstring in api/services/profile.py. The client must render
    the null case, not treat it as zero."""

    streak_days: int
    readiness: float | None
    readiness_sample: int
    readiness_min_sample: int
    # Freezes in hand. A streak that can survive one bad evening is one people keep going
    # rather than abandon, and the count has to be visible or it protects nobody
    # psychologically — the point is knowing you are covered.
    streak_freezes: int = 0
    pass_accuracy: float
    exams: ExamSummaryOut


# --- vocabulary trainer -----------------------------------------------------


class VocabItemOut(BaseModel):
    """One thing to answer. Carries the prompt and NOT the answer — see
    api/services/vocab.py for why the expected answer never ships with the round."""

    term_id: int
    direction: str = Field(description=f"one of {VOCAB_DIRECTIONS}")
    prompt: str
    answer_lang: str


class VocabRoundOut(BaseModel):
    lang: str
    size: int
    items: list[VocabItemOut]


class VocabAnswerIn(BaseModel):
    term_id: int
    direction: str
    given: str = Field(default="", max_length=200)


class VocabAnswerOut(BaseModel):
    term_id: int
    verdict: str = Field(description="correct | almost | wrong")
    expected: str
    correction: str | None
    it: str
    gloss: str
    box: int


class VocabTermOut(BaseModel):
    id: int
    rank: int
    it: str
    gloss: str
    box: int = Field(description="0 when never answered")


class VocabListOut(BaseModel):
    lang: str
    total: int
    offset: int
    terms: list[VocabTermOut]


class VocabStatsOut(BaseModel):
    total: int
    started: int
    learned: int
    almost: int


class ResetPreviewOut(BaseModel):
    """What a reset would destroy. Shown in the confirmation, because "this will delete
    your progress" is a sentence people click past and "412 answers and 6 exams" is not."""

    answers: int
    questions: int
    sittings: int
    words: int
