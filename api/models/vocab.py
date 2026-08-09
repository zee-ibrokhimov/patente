"""The driving-vocabulary trainer: the word list, and one learner's state against it.

Why this is a separate table from `translations` even though both hold Italian and its
renderings: a translation belongs to a *question* and exists to be read underneath it. A
vocabulary term belongs to nothing and exists to be *produced from memory*. The learner
types it, in both directions, and is graded. Those are different objects with different
lifecycles — the word list is fixed content, translations are generated on demand and
cost money each time.

Provenance of the list: a 1100-word Italian/English sheet supplied by the owner, ranked
by frequency in the theory exam. Its English was machine-translated and wrong on roughly
half the entries ("a raso" -> "At satin", which is `raso` the fabric, not the fixed phrase
meaning "flush with the surface"). So every gloss here was regenerated FROM THE ITALIAN
with the sheet's English passed only as a disambiguation hint — see
content/seed_vocab.py. Translating the English would have carried each error into three
languages at once.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, ForeignKey, Index, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from api.models.base import Base, utcnow


class VocabTerm(Base):
    """One item of exam vocabulary, with its gloss in every language served.

    The glosses are stored as plain text and may offer alternatives separated by a
    comma — "звуковой сигнал, клаксон". Both are right, and the grader accepts either;
    see api/services/vocab_grading.py. Do not "clean" these into a single form: a
    learner who wrote the second one is not wrong, and marking them wrong for it is how
    a vocabulary app loses trust.
    """

    __tablename__ = "vocab_terms"
    __table_args__ = (
        UniqueConstraint("it", name="uq_vocab_term_it"),
        # The drill draws in teaching order — commonest words first — so this is the
        # ordering read on every round.
        Index("ix_vocab_rank", "rank"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    # Position in the owner's frequency-ranked sheet. Lower is more common, and more
    # worth learning first. Not contiguous: ten Italian words appear twice in the sheet
    # and are stored once.
    rank: Mapped[int] = mapped_column()

    it: Mapped[str] = mapped_column(Text)
    en: Mapped[str] = mapped_column(Text)
    ru: Mapped[str] = mapped_column(Text)
    uz: Mapped[str] = mapped_column(Text)

    progress: Mapped[list[VocabProgress]] = relationship(
        back_populates="term", cascade="all, delete-orphan"
    )

    def gloss(self, lang: str) -> str:
        """The rendering in `lang`. Italian falls back to English, because a vocabulary
        test needs two different languages and it/it is not a question."""
        return {"en": self.en, "ru": self.ru, "uz": self.uz}.get(lang, self.en)


class VocabProgress(Base):
    """Leitner state for one learner against one term.

    Deliberately the same five-box scheme as `progress` uses for questions, rather than
    a second invented one: a learner has one sense of "I keep getting this wrong", and
    two different spacing algorithms in one app would make the two halves feel unrelated.

    Direction is NOT tracked separately. Knowing `sosta -> parking` and being able to
    produce `sosta` from `parking` are genuinely different skills, and a stricter design
    would box them apart. It is one row on purpose: doubling the state doubles the drill
    length for a list this long, and the round mixes directions anyway, so both get
    exercised. If recall in one direction turns out to lag badly, split it then — with
    data, rather than now on a guess.
    """

    __tablename__ = "vocab_progress"
    __table_args__ = (
        # The hot path, exactly as for questions: "what is due for this learner now".
        Index("ix_vocab_progress_due", "chat_id", "due_at"),
    )

    chat_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.chat_id", ondelete="CASCADE"), primary_key=True
    )
    term_id: Mapped[int] = mapped_column(
        ForeignKey("vocab_terms.id", ondelete="CASCADE"), primary_key=True
    )

    box: Mapped[int] = mapped_column(default=1)
    due_at: Mapped[datetime] = mapped_column(default=utcnow)
    seen: Mapped[int] = mapped_column(default=0)
    wrong: Mapped[int] = mapped_column(default=0)

    # Counted apart from `wrong` because it means something different: the learner knew
    # the word and missed the ending. It is the number that tells you whether someone is
    # failing on vocabulary or on grammar, and those need different help.
    almost: Mapped[int] = mapped_column(default=0)

    last_answer_at: Mapped[datetime | None] = mapped_column(default=None)

    term: Mapped[VocabTerm] = relationship(back_populates="progress")
