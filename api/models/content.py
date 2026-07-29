"""Ministerial content: topics, quesiti, questions, figures, translations, explanations.

Everything here is derived from the official listato or generated offline from it.
It is rebuilt by content/seed.py and never written at runtime.

Deviations from the plan's §14.3 sketch, and why:

  · `topics` is its own table rather than a string column on questions. The
    ministerial topic names run to 250 characters and would otherwise be repeated
    across 7106 rows.

  · `quesiti` is kept as a table. The listato's grouping of statements around one
    figure is real structure — it drives "another question about this sign" and
    gives cluster.py a natural starting point.

  · `figures` is keyed by path, not owned by a question. 409 distinct figures serve
    3946 statements, so the Telegram file_id cache (§6.4) belongs here — cached
    once per figure, not once per question.

  · `image_path` lives on the question, not the quesito. Comparison statements
    ("il segnale (A) può essere abbinato al segnale (B)") carry their own composite
    figure that differs from the rest of their group — 139 of them in this listato.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Index, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from api.models.base import Base, utcnow


class Topic(Base):
    __tablename__ = "topics"

    # 1..25, assigned by extract.py in stable alphabetical order.
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(Text, unique=True)

    questions: Mapped[list[Question]] = relationship(back_populates="topic")


class Figure(Base):
    __tablename__ = "figures"

    # Relative path as written by extract.py, e.g. "images/e90fb0585f52aad1.jpeg".
    path: Mapped[str] = mapped_column(Text, primary_key=True)
    sha256: Mapped[str] = mapped_column(Text, index=True)

    # Filled the first time the bot uploads this figure. Re-uploading sign images
    # to thousands of users would otherwise be the entire bandwidth bill.
    telegram_file_id: Mapped[str | None] = mapped_column(Text, default=None)


class Cluster(Base):
    """A rule, not a statement.

    A large share of the bank is the same rule reworded. Explanations key off the
    cluster so one approved explanation serves every variant and a correction
    lands in exactly one row. Populated by content/cluster.py at build step 6;
    questions.cluster_id stays NULL until then.
    """

    __tablename__ = "clusters"

    id: Mapped[int] = mapped_column(primary_key=True)
    topic_id: Mapped[int | None] = mapped_column(ForeignKey("topics.id"), index=True)
    rule_summary: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Quesito(Base):
    """One ministerial group: a figure (usually) and the statements about it."""

    __tablename__ = "quesiti"

    id: Mapped[int] = mapped_column(primary_key=True)
    topic_id: Mapped[int] = mapped_column(ForeignKey("topics.id"), index=True)

    # The figure most of the group's statements share. Advisory only — the
    # authoritative figure for a question is always Question.image_path.
    primary_image: Mapped[str | None] = mapped_column(ForeignKey("figures.path"))


class Question(Base):
    __tablename__ = "questions"

    # Ministerial statement number. Stable across listato reissues, which is what
    # makes a diff-based reseed possible.
    id: Mapped[int] = mapped_column(primary_key=True)
    quesito_id: Mapped[int] = mapped_column(ForeignKey("quesiti.id"), index=True)
    topic_id: Mapped[int] = mapped_column(ForeignKey("topics.id"), index=True)

    stem_it: Mapped[str | None] = mapped_column(Text, default=None)
    statement_it: Mapped[str] = mapped_column(Text)
    answer: Mapped[bool]
    image_path: Mapped[str | None] = mapped_column(ForeignKey("figures.path"), default=None)

    cluster_id: Mapped[int | None] = mapped_column(ForeignKey("clusters.id"), index=True)
    source_version: Mapped[str] = mapped_column(Text, index=True)

    topic: Mapped[Topic] = relationship(back_populates="questions")
    translations: Mapped[list[Translation]] = relationship(
        back_populates="question", cascade="all, delete-orphan"
    )


class Translation(Base):
    """A comprehension aid shown underneath the Italian, never a replacement.

    Users train to recognise the exact ministerial phrasing, so the Italian is
    always what gets displayed as the question.
    """

    __tablename__ = "translations"
    __table_args__ = (UniqueConstraint("question_id", "lang", name="uq_translation_lang"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    question_id: Mapped[int] = mapped_column(ForeignKey("questions.id", ondelete="CASCADE"))
    lang: Mapped[str] = mapped_column(Text, index=True)
    stem: Mapped[str | None] = mapped_column(Text, default=None)
    statement: Mapped[str] = mapped_column(Text)
    reviewed_at: Mapped[datetime | None] = mapped_column(default=None)

    question: Mapped[Question] = relationship(back_populates="translations")


class Explanation(Base):
    __tablename__ = "explanations"
    __table_args__ = (
        UniqueConstraint("cluster_id", "lang", name="uq_explanation_lang"),
        # The paid read path is "give me the approved explanation for this
        # cluster in this language" — this index is that query.
        Index("ix_explanation_lookup", "cluster_id", "lang", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    cluster_id: Mapped[int] = mapped_column(ForeignKey("clusters.id", ondelete="CASCADE"))
    lang: Mapped[str] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)

    # See shared.constants.EXPLANATION_STATUSES. Only "approved" is ever served.
    status: Mapped[str] = mapped_column(Text, default="draft", index=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(default=None)
    reviewer: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
