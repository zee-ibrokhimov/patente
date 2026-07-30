"""Per-user state: users, progress, purchases, events, reports.

This is the only irreplaceable data in the system — content can be regenerated
from the listato, entitlement and progress cannot. It is also the only place
personal data lives, and the answer to "what personal data?" is deliberately
"a Telegram chat id and an answer history". No names, usernames or message text
are stored anywhere, which is what makes /delete a single cascading delete.

Deviations from the plan's §14.3 sketch, and why:

  · `progress` gets an explicit composite primary key (chat_id, question_id). The
    sketch had no key at all, which would have allowed a user to accumulate
    duplicate rows for the same question and quietly corrupt their own Leitner
    scheduling.

  · `events.payload` is a JSON column rather than free text, so the §9 metrics
    stay queryable without a migration or a parsing pass.

  · `purchases.tribute_purchase_id` is UNIQUE and NOT NULL. That constraint is
    the webhook idempotency guarantee: a redelivered payment fails to insert
    instead of extending the pass a second time.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, BigInteger, ForeignKey, Index, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from api.models.base import Base, utcnow


class User(Base):
    __tablename__ = "users"

    # Telegram chat id. Exceeds 32 bits for newer accounts, hence BigInteger.
    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    lang: Mapped[str] = mapped_column(Text, default="ru")
    translations_on: Mapped[bool] = mapped_column(default=True)

    # NULL means "never bought". Access is granted while this is in the future;
    # on expiry, progress is kept and only translations and explanations lock.
    pass_expires_at: Mapped[datetime | None] = mapped_column(default=None, index=True)

    # Lifetime taster (plan §4.3). Quality is the entire pitch, so a user has to
    # see a good explanation before being asked to pay for them.
    free_explanations_used: Mapped[int] = mapped_column(default=0)

    onboarded_at: Mapped[datetime | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    progress: Mapped[list[Progress]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Progress(Base):
    """Leitner state, one row per (user, question) actually seen."""

    __tablename__ = "progress"
    __table_args__ = (
        # The hot path: "what is due for this user right now".
        Index("ix_progress_due", "chat_id", "due_at"),
    )

    chat_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.chat_id", ondelete="CASCADE"), primary_key=True
    )
    question_id: Mapped[int] = mapped_column(ForeignKey("questions.id"), primary_key=True)

    box: Mapped[int] = mapped_column(default=1)
    due_at: Mapped[datetime] = mapped_column(default=utcnow)
    seen: Mapped[int] = mapped_column(default=0)
    wrong: Mapped[int] = mapped_column(default=0)
    last_answer_at: Mapped[datetime | None] = mapped_column(default=None)

    user: Mapped[User] = relationship(back_populates="progress")


class Purchase(Base):
    __tablename__ = "purchases"

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)

    # Tribute's id for the payment. UNIQUE is doing real work here: webhook
    # redelivery is normal, and without it a retried delivery would extend the
    # pass again. Refunds are matched on this too.
    tribute_purchase_id: Mapped[str] = mapped_column(Text, unique=True)

    tier: Mapped[str] = mapped_column(Text)
    amount_cents: Mapped[int]
    currency: Mapped[str] = mapped_column(Text, default="EUR")

    # What the pass was extended to when this purchase was applied. Stacking
    # extends from the current expiry, not from today, so this is not derivable
    # after the fact.
    extended_to: Mapped[datetime | None] = mapped_column(default=None)

    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    refunded_at: Mapped[datetime | None] = mapped_column(default=None)


class Event(Base):
    """Append-only analytics log (plan §9). Never read on the hot path."""

    __tablename__ = "events"
    __table_args__ = (
        Index("ix_event_type_time", "type", "created_at"),
        Index("ix_event_chat_time", "chat_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    type: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict | None] = mapped_column(JSON, default=None)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Report(Base):
    """"This explanation is wrong" from the in-bot report button.

    Volume per 1,000 questions served is a headline quality metric, and these are
    the first place to look when the correction rate is being estimated.
    """

    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    question_id: Mapped[int] = mapped_column(ForeignKey("questions.id"))
    cluster_id: Mapped[int | None] = mapped_column(ForeignKey("clusters.id"), default=None)
    lang: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(default=None)


class QuizSession(Base):
    """A bounded, gradeable sitting — an exam or a practice run.

    The exam paper is FROZEN at creation rather than served question by question, and
    that one decision buys four things at once:

      · no repeats. Serving one at a time via `selection.next_question` would re-serve
        the exam's own misses: box 1 is a 10-minute interval, the exam runs 20 minutes,
        and selection orders strictly by `due_at`, so a question missed at minute 2 is
        the top candidate again at minute 12.
      · resumability. The Mini App persists nothing across a reopen, so a client-held
        paper is lost the moment the user backgrounds Telegram.
      · server-side grading, with no need to trust anything the client sends back.
      · the whole paper can ship in one response, which removes thirty blocking round
        trips from a screen with a clock running on it.

    `expires_at` is computed and enforced HERE. A countdown held by the client is
    editable by anyone who can open devtools, and the Mini App's only identity is
    initData — there is no session cookie to bind a deadline to.
    """

    __tablename__ = "quiz_sessions"
    __table_args__ = (
        # "does this user have something open right now", asked on every app open.
        Index("ix_session_open", "chat_id", "state"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.chat_id", ondelete="CASCADE"), index=True
    )
    mode: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text, default="open")

    started_at: Mapped[datetime] = mapped_column(default=utcnow)
    # Null for practice, which has no clock.
    expires_at: Mapped[datetime | None] = mapped_column(default=None)
    finished_at: Mapped[datetime | None] = mapped_column(default=None)

    # The rules THIS sitting was graded under, copied at creation rather than read from
    # constants at grading time. Plan §11 leaves the format open (30 vs 40 questions),
    # so changing it later must not silently re-grade or misreport an exam already sat.
    question_count: Mapped[int] = mapped_column(default=0)
    max_errors: Mapped[int | None] = mapped_column(default=None)

    # Denormalised at finish so history and stats never re-walk the items.
    answered: Mapped[int] = mapped_column(default=0)
    wrong: Mapped[int] = mapped_column(default=0)
    passed: Mapped[bool | None] = mapped_column(default=None)

    user: Mapped[User] = relationship()
    items: Mapped[list[QuizSessionItem]] = relationship(
        back_populates="session", cascade="all, delete-orphan", order_by="QuizSessionItem.ordinal"
    )


class QuizSessionItem(Base):
    """One question on the paper, and the answer given to it.

    Keyed on (session_id, ordinal), NOT on (session_id, question_id). Practice mode has
    no fixed paper and a wrongly-answered question is *supposed* to come back within the
    same sitting — a uniqueness constraint on the question would turn that into an
    IntegrityError mid-session. Distinctness of an exam paper is enforced where it is
    actually wanted, in `selection.exam_paper`.
    """

    __tablename__ = "quiz_session_items"

    session_id: Mapped[int] = mapped_column(
        ForeignKey("quiz_sessions.id", ondelete="CASCADE"), primary_key=True
    )
    ordinal: Mapped[int] = mapped_column(primary_key=True)
    question_id: Mapped[int] = mapped_column(ForeignKey("questions.id"), index=True)

    # Null until answered. `given` is the user's Vero/Falso; `correct` is stored rather
    # than derived so a results screen never re-reads the answer key, and so a later
    # correction to the bank cannot silently rewrite a past exam result.
    given: Mapped[bool | None] = mapped_column(default=None)
    correct: Mapped[bool | None] = mapped_column(default=None)
    answered_at: Mapped[datetime | None] = mapped_column(default=None)

    session: Mapped[QuizSession] = relationship(back_populates="items")
