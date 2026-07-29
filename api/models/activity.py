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
