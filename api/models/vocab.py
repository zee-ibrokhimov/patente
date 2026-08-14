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

from sqlalchemy import BigInteger, ForeignKey, Index, Text, text
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
        # TWO uniqueness rules, not one. A plain UNIQUE(owner_chat_id, it) would not keep
        # the shared list unique, because SQLite treats NULLs as distinct and would accept
        # two shared rows for `sosta`. And UNIQUE(it) alone — which is what shipped — stops
        # a learner adding their own note on a word the glossary already has.
        Index("uq_vocab_shared_it", "it", unique=True,
              sqlite_where=text("owner_chat_id IS NULL")),
        Index("uq_vocab_own_it", "owner_chat_id", "it", unique=True,
              sqlite_where=text("owner_chat_id IS NOT NULL")),
        # The drill draws in teaching order — commonest words first — so this is the
        # ordering read on every round.
        Index("ix_vocab_rank", "rank"),
        Index("ix_vocab_owner", "owner_chat_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    # NULL for the SHARED glossary, a chat id for one learner's own word.
    #
    # One table rather than two: a second one would need its own progress rows, its own
    # Leitner scheduling, its own place in the round draw and in the flip-card deck, and
    # each of those is a chance for the two kinds of word to drift apart. Sharing the table
    # means a learner's own words are drawn, scheduled and graded by the code that already
    # works. The cost is that every query must be scoped — see `vocab.visible_to`, and the
    # test that fails when a query is written without it.
    owner_chat_id: Mapped[int | None] = mapped_column(BigInteger, default=None)

    # Position in the shared frequency-ranked sheet. Lower is more common, and more worth
    # learning first. Not contiguous: ten Italian words appear twice in the sheet and are
    # stored once.
    #
    # NULL for a learner's own word, which has no position in a frequency list — and NULLs
    # sort first, which is where somebody's own additions belong in their own list.
    rank: Mapped[int | None] = mapped_column(default=None)

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


class WordGloss(Base):
    """The shared translation memory for words tapped inside a question.

    NOT THE GLOSSARY, and the separation is the point. `vocab_terms` with a NULL owner is a
    curated, frequency-ranked sheet of 1,104 exam words that the drill draws from in teaching
    order — dumping every word anybody ever tapped into it would destroy both the curation
    and the ordering. This is a cache of "what does this Italian word mean", nothing more,
    and the drill never reads it.

    WHY IT IS SHARED WHEN THE SAVED WORDS ARE PERSONAL

    Measured against the real bank before this was built: the glossary covers only 14.5% of
    the word tokens in the questions, and the words a learner is most likely to tap are the
    ones missing from it — `raffigurato` appears 2,796 times and is not there, nor are
    `veicolo`, `veicoli` or `velocità`. So nearly every tap would be a model call.

    But there are only 5,239 distinct words in the entire bank. Cached and shared, the first
    learner to tap `raffigurato` pays for it and every learner after that gets it instantly.
    The worst case is the whole bank translated once, ever, rather than a cost that grows
    with users.

    KEYED ON THE DICTIONARY FORM, not on what was tapped. `veicolo` and `veicoli` are two
    tokens and one word; keying on the surface form would double the cache and fill learners'
    vocabularies with duplicates of the same noun. The model returns the lemma, which is what
    a dictionary would do and what a stemmer would get wrong on Italian.
    """

    __tablename__ = "word_glosses"

    # The dictionary form, lowercased. Primary key rather than an id: every read is "do we
    # already know this word", and a surrogate key would need a unique index over this
    # column anyway.
    lemma: Mapped[str] = mapped_column(Text, primary_key=True)

    en: Mapped[str] = mapped_column(Text)
    ru: Mapped[str] = mapped_column(Text)
    uz: Mapped[str] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    def gloss(self, lang: str) -> str:
        return {"en": self.en, "ru": self.ru, "uz": self.uz}.get(lang, self.en)


class WordForm(Base):
    """Every surface form ever tapped, pointing at its dictionary form.

    THE CACHE IS USELESS WITHOUT THIS, and it took a test to notice. The glosses are keyed on
    the lemma — `raffigurare` — but a learner taps `raffigurato`, which is a different string.
    Looking the cache up by what was tapped therefore missed on every inflected word, so the
    second learner to tap `raffigurato` paid for it again, and the third, and the fourth. The
    text they got back was identical and correct; only the bill was wrong, which is the kind
    of defect that is invisible until somebody reads an invoice.

    One row per form, so a word with six inflections costs ONE model call and five cheap
    inserts. `raffigurato`, `raffigurata` and `raffigurati` all arrive at `raffigurare`.
    """

    __tablename__ = "word_forms"

    # What was tapped, lowercased and trimmed. The key, because this table exists to be
    # looked up by exactly the string the client sends.
    form: Mapped[str] = mapped_column(Text, primary_key=True)
    lemma: Mapped[str] = mapped_column(ForeignKey("word_glosses.lemma", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


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
