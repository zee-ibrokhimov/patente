"""The vocabulary trainer: choosing what to ask, and recording the answer.

Two rules shape everything here.

**The expected answer never leaves the server before it is graded.** A round hands the
client a prompt and nothing else. If the paper carried its own answers — the obvious,
convenient design — then anybody could read them out of the network tab, and a test you
can trivially pass teaches nothing. This is the same reason exam mode reveals nothing
until it is submitted.

**Vocabulary is Premium.** It is the feature the paywall has been advertising as "coming
soon" in four languages, so the gate lives in one place, `entitlement.can_use_vocabulary`,
and is checked in this module rather than in a route — routes are a surface, not a policy.
"""

from __future__ import annotations

import logging
import random
from datetime import datetime, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.models import User, VocabProgress, VocabTerm
from api.services import events, leitner
from api.services.entitlement import Entitlement
from api.services.vocab_grading import Grade, Verdict, grade
from shared.constants import (
    EV_VOCAB_ADDED,
    EV_VOCAB_RECALL,
    TRANSLATION_LANGUAGES,
    VOCAB_IT_TO_LANG,
    VOCAB_LANG_TO_IT,
    VOCAB_PAIR_FALLBACK,
    VOCAB_ROUND_SIZE,
)

def visible_to(chat_id: int):
    """Which terms this learner may see: the shared glossary, plus their own.

    ONE helper rather than a filter written out at each call site, because there are a
    dozen of them and forgetting one does not fail — it silently shows one learner another
    learner's private words. `test_no_vocab_query_forgets_the_owner` scans this module and
    fails on any `select(VocabTerm)` that does not go through here.
    """
    return or_(VocabTerm.owner_chat_id.is_(None), VocabTerm.owner_chat_id == chat_id)


# A learner's own word is short by nature — "sosta", "avvisatore acustico". The cap is
# generous against the longest term in the shared sheet (34 characters) and small enough
# that the column stays a vocabulary list.
OWN_MAX_LENGTH = 120

# Enough to be somebody's whole personal glossary, few enough that one account cannot fill
# the table. The shared sheet is 1,104 words; nobody is hand-typing more than this.
OWN_MAX_TERMS = 500


async def add_own(
    session: AsyncSession, user, it: str, gloss: str, entitlement: Entitlement
) -> VocabTerm:
    """A word the learner adds for themselves.

    Stored in the same table as the shared glossary, with an owner — so it is drawn into
    rounds, scheduled by Leitner, graded and counted by exactly the code that already works.

    THE GLOSS GOES IN ALL THREE LANGUAGE COLUMNS, deliberately. A shared term carries a real
    translation per language; a learner's own note is one string they wrote, and they are
    the only person who will ever read it. Writing it once and leaving the other two empty
    would make their own word vanish from their own list the moment they switched the
    interface language — which is a bug that would look like data loss.
    """
    _require_access(entitlement)

    word = (it or "").strip()
    meaning = (gloss or "").strip()
    if not word or not meaning:
        raise VocabError(422, "both the word and its meaning are needed")
    if len(word) > OWN_MAX_LENGTH or len(meaning) > OWN_MAX_LENGTH:
        raise VocabError(422, f"keep each side under {OWN_MAX_LENGTH} characters")

    mine = await session.scalar(
        select(func.count()).select_from(VocabTerm).where(own_only(user.chat_id))) or 0
    if mine >= OWN_MAX_TERMS:
        raise VocabError(422, "that is as many words as one list can hold")

    clash = await session.scalar(
        select(VocabTerm).where(own_only(user.chat_id),
                                func.lower(VocabTerm.it) == word.lower()))
    if clash is not None:
        raise VocabError(409, "you have already added that word")

    row = VocabTerm(owner_chat_id=user.chat_id, rank=None, it=word,
                    en=meaning, ru=meaning, uz=meaning)
    session.add(row)
    await session.flush()
    await events.record(session, EV_VOCAB_ADDED, chat_id=user.chat_id, term_id=row.id)
    return row


async def edit_own(
    session: AsyncSession, user, term_id: int, it: str | None, gloss: str | None,
    entitlement: Entitlement,
) -> VocabTerm:
    """Change a word the learner added. Shared terms are not editable — see `own_only`."""
    _require_access(entitlement)

    row = await session.scalar(
        select(VocabTerm).where(VocabTerm.id == term_id, own_only(user.chat_id)))
    if row is None:
        # 404 whether it does not exist, belongs to somebody else, or is a shared term:
        # all three are "not yours to change", and distinguishing them tells the caller
        # something about rows they cannot see.
        raise VocabError(404, "no such word in your list")

    if it is not None:
        word = it.strip()
        if not word:
            raise VocabError(422, "the word cannot be empty")
        if len(word) > OWN_MAX_LENGTH:
            raise VocabError(422, f"keep each side under {OWN_MAX_LENGTH} characters")
        row.it = word
    if gloss is not None:
        meaning = gloss.strip()
        if not meaning:
            raise VocabError(422, "the meaning cannot be empty")
        if len(meaning) > OWN_MAX_LENGTH:
            raise VocabError(422, f"keep each side under {OWN_MAX_LENGTH} characters")
        row.en = row.ru = row.uz = meaning

    await session.flush()
    return row


async def delete_own(
    session: AsyncSession, user, term_id: int, entitlement: Entitlement
) -> None:
    """Remove a word the learner added, and its Leitner history with it.

    The progress rows go through the relationship's delete-orphan cascade rather than a
    second statement here: a term with no row and a schedule that still refers to it is the
    kind of leftover that surfaces months later as a round containing a blank card.
    """
    _require_access(entitlement)

    row = await session.scalar(
        select(VocabTerm).where(VocabTerm.id == term_id, own_only(user.chat_id)))
    if row is None:
        raise VocabError(404, "no such word in your list")
    await session.delete(row)
    await session.flush()


async def _visible_term(session: AsyncSession, user, term_id: int) -> VocabTerm:
    """A term this learner may act on, or 404.

    404 and not 403 for somebody else's private word: telling them it exists is already
    telling them something about another learner.
    """
    term = await session.scalar(
        select(VocabTerm).where(VocabTerm.id == term_id, visible_to(user.chat_id)))
    if term is None:
        raise VocabError(404, "no such term")
    return term


def own_only(chat_id: int):
    """Just this learner's own additions — for editing and deleting, where touching a
    shared row would be a very different thing."""
    return VocabTerm.owner_chat_id == chat_id


log = logging.getLogger(__name__)


class VocabError(Exception):
    """Something the caller did, with the status a route should return for it."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def pair_language(user: User) -> str:
    """Which language the Italian is tested against.

    An Italian-speaking user cannot be tested it/it, so they get English — the same
    reasoning that keeps Italian out of TRANSLATION_LANGUAGES. Anything unrecognised
    lands on the same fallback rather than raising, because a user row with an odd
    language should degrade to a working screen, not a 500.
    """
    return user.lang if user.lang in TRANSLATION_LANGUAGES else VOCAB_PAIR_FALLBACK


def _require_access(entitlement: Entitlement) -> None:
    if not entitlement.can_use_vocabulary:
        raise VocabError(402, "vocabulary is part of Premium")


async def round_for(
    session: AsyncSession,
    user: User,
    entitlement: Entitlement,
    now: datetime | None = None,
    size: int = VOCAB_ROUND_SIZE,
) -> dict:
    """Build one round: what is due, topped up with what has never been seen.

    Due-first, then new, is the ordering that matters. A learner who has 200 words in
    flight and keeps being shown fresh ones never consolidates any of them; the words
    they are about to forget are worth more than the words they have not met.
    """
    _require_access(entitlement)
    now = now or datetime.now(timezone.utc)
    lang = pair_language(user)

    due = (await session.scalars(
        select(VocabTerm)
        .join(VocabProgress, VocabProgress.term_id == VocabTerm.id)
        .where(VocabProgress.chat_id == user.chat_id, VocabProgress.due_at <= now,
               visible_to(user.chat_id))
        .order_by(VocabProgress.due_at)
        .limit(size)
    )).all()

    terms = list(due)
    if len(terms) < size:
        seen_ids = select(VocabProgress.term_id).where(VocabProgress.chat_id == user.chat_id)
        fresh = (await session.scalars(
            select(VocabTerm)
            .where(VocabTerm.id.not_in(seen_ids), visible_to(user.chat_id))
            # NULLs first, so a learner's own additions lead their own drill; then the
            # shared sheet in teaching order, commonest first.
            .order_by(VocabTerm.rank.is_(None).desc(), VocabTerm.rank)
            .limit(size - len(terms))
        )).all()
        terms.extend(fresh)

    if not terms:
        # Everything is learned and nothing is due yet. Better than an empty screen:
        # show the words closest to falling due, so the round is never a dead end.
        terms = list((await session.scalars(
            select(VocabTerm)
            .join(VocabProgress, VocabProgress.term_id == VocabTerm.id)
            .where(VocabProgress.chat_id == user.chat_id, visible_to(user.chat_id))
            .order_by(VocabProgress.due_at)
            .limit(size)
        )).all())

    # Alternate direction rather than randomise it: over twenty items a coin flip
    # routinely produces a run of eight in one direction, which reads as a broken mix.
    # Shuffling the terms first keeps the pairing itself unpredictable.
    rng = random.Random()
    rng.shuffle(terms)

    items = []
    for i, term in enumerate(terms):
        direction = VOCAB_IT_TO_LANG if i % 2 == 0 else VOCAB_LANG_TO_IT
        prompt = term.it if direction == VOCAB_IT_TO_LANG else term.gloss(lang)
        # A term with an empty gloss cannot be asked in either direction without
        # grading every answer wrong. Seeding drops these, so this is belt and braces.
        if not prompt.strip():
            continue
        items.append({
            "term_id": term.id,
            "direction": direction,
            "prompt": prompt,
            # Which language the learner must ANSWER in, so the keyboard hint and the
            # placeholder can be right.
            "answer_lang": lang if direction == VOCAB_IT_TO_LANG else "it",
        })

    return {"lang": lang, "size": len(items), "items": items}


async def answer(
    session: AsyncSession,
    user: User,
    term_id: int,
    direction: str,
    given: str,
    entitlement: Entitlement,
    now: datetime | None = None,
) -> dict:
    """Grade one typed answer and move the term through the Leitner boxes."""
    _require_access(entitlement)
    now = now or datetime.now(timezone.utc)

    if direction not in (VOCAB_IT_TO_LANG, VOCAB_LANG_TO_IT):
        raise VocabError(422, f"direction must be one of {(VOCAB_IT_TO_LANG, VOCAB_LANG_TO_IT)}")

    term = await _visible_term(session, user, term_id)

    lang = pair_language(user)
    if direction == VOCAB_IT_TO_LANG:
        expected, answer_lang = term.gloss(lang), lang
        result = grade(given or "", expected, lang=answer_lang)
    else:
        # ASKING FOR THE ITALIAN, where more than one answer is genuinely right.
        #
        # Italian words share glosses constantly: `conducente` and `autista` are both
        # "водитель"; `preavvisa` and `avverte` are both "warns". Grading only against the
        # word we happen to have picked marks a correct translation WRONG — measured on
        # the shipped list, 105 Italian words share a Russian gloss, 117 an English one,
        # 177 an Uzbek one. Being told you are wrong when you are right is the fastest way
        # to stop trusting a trainer, and it would have been 10-16% of every reverse card.
        expected, answer_lang = term.it, "it"
        accepted = await _same_meaning(session, term, lang)
        result = _best_of(given or "", accepted, expected)

    row = await session.get(VocabProgress, (user.chat_id, term_id))
    if row is None:
        # Spelled out rather than relying on the column defaults: those are applied by
        # the database at INSERT, so a freshly constructed row still has None in every
        # counter and the increments below would fail on the learner's first answer.
        row = VocabProgress(chat_id=user.chat_id, term_id=term_id, box=1,
                            due_at=now, seen=0, wrong=0, almost=0)
        session.add(row)

    row.seen += 1
    if result.verdict is Verdict.ALMOST:
        row.almost += 1
    elif result.verdict is Verdict.WRONG:
        row.wrong += 1

    row.box, row.due_at = leitner.schedule(row.box, result.is_progress, now)
    row.last_answer_at = now

    return {
        "term_id": term.id,
        "verdict": result.verdict.value,
        # Always sent, for every verdict. The learner has just committed to an answer;
        # withholding the right one now is the moment they were going to learn from.
        "expected": expected,
        "correction": result.correction,
        "it": term.it,
        "gloss": term.gloss(lang),
        "box": row.box,
    }


async def _same_meaning(session: AsyncSession, term: VocabTerm, lang: str) -> list[str]:
    """Every Italian word carrying this term's gloss — all of them correct answers."""
    column = {"en": VocabTerm.en, "ru": VocabTerm.ru, "uz": VocabTerm.uz}.get(lang)
    if column is None:
        return [term.it]
    rows = await session.scalars(
        select(VocabTerm.it).where(
            func.lower(func.trim(column)) == term.gloss(lang).strip().lower(),
            visible_to(term.owner_chat_id or -1) if term.owner_chat_id else
            VocabTerm.owner_chat_id.is_(None))
    )
    # The asked-about word first, so it is the one shown as the correction.
    others = [r for r in rows if r != term.it]
    return [term.it, *others]


def _best_of(given: str, accepted: list[str], shown: str):
    """Grade against every acceptable Italian and keep the kindest verdict.

    A near-miss on a synonym still beats a flat WRONG, but the correction always names the
    word the card asked about — correcting someone to a synonym they were not shown would
    be answering a question nobody asked.
    """
    best = None
    for candidate in accepted:
        result = grade(given, candidate, lang="it")
        if result.verdict is Verdict.CORRECT:
            return Grade(Verdict.CORRECT, correction=None, matched=candidate)
        if best is None or (result.verdict is Verdict.ALMOST and best.verdict is Verdict.WRONG):
            best = result
    if best is None:
        return Grade(Verdict.WRONG, correction=shown, matched=None)
    return Grade(best.verdict, correction=shown, matched=best.matched)


async def browse(
    session: AsyncSession,
    user: User,
    entitlement: Entitlement,
    query: str = "",
    offset: int = 0,
    limit: int = 50,
) -> dict:
    """The word list itself, searchable — the reference half of the feature.

    Search runs over the Italian AND the learner's own language, because someone who
    half-remembers "обгон" and wants the Italian for it is doing exactly what this list
    is for.
    """
    _require_access(entitlement)
    lang = pair_language(user)
    column = {"en": VocabTerm.en, "ru": VocabTerm.ru, "uz": VocabTerm.uz}[lang]

    stmt = select(VocabTerm).where(visible_to(user.chat_id))
    count_stmt = (select(func.count()).select_from(VocabTerm)
                  .where(visible_to(user.chat_id)))
    q = (query or "").strip()
    if q:
        like = f"%{q}%"
        where = or_(VocabTerm.it.ilike(like), column.ilike(like))
        stmt, count_stmt = stmt.where(where), count_stmt.where(where)

    total = (await session.scalar(count_stmt)) or 0
    rows = (await session.scalars(
        # NULLs first: a learner's own additions lead their own list. They are the words
        # that person just chose to care about, and burying them under 1,104 shared entries
        # is the difference between a feature and a form nobody revisits.
        #
        # Written out even though SQLite already sorts NULLs first, because that is a
        # BACKEND DEFAULT and not a promise — Postgres sorts them last. Mutation reports
        # this clause as removable, and on SQLite it is; the day the database moves it
        # would silently invert the one ordering this feature depends on.
        stmt.order_by(VocabTerm.rank.is_(None).desc(), VocabTerm.rank)
        .offset(max(0, offset)).limit(min(200, max(1, limit)))
    )).all()

    known = {
        p.term_id: p
        for p in (await session.scalars(
            select(VocabProgress).where(
                VocabProgress.chat_id == user.chat_id,
                VocabProgress.term_id.in_([r.id for r in rows] or [0]),
            )
        )).all()
    }

    return {
        "lang": lang,
        "total": total,
        "offset": offset,
        "terms": [
            {
                "id": r.id, "rank": r.rank, "it": r.it, "gloss": r.gloss(lang),
                "mine": r.owner_chat_id is not None,
                "box": known[r.id].box if r.id in known else 0,
            }
            for r in rows
        ],
    }


async def stats(session: AsyncSession, user: User) -> dict:
    """How far through the list this learner is.

    Free of charge and outside the gate, deliberately: this is the number that makes
    the feature worth paying for, and hiding it behind the thing it advertises is a
    poor trade. It is also the same call the Profile screen makes.
    """
    total = (await session.scalar(
        select(func.count()).select_from(VocabTerm)
        .where(visible_to(user.chat_id)))) or 0
    rows = (await session.scalars(
        select(VocabProgress).where(VocabProgress.chat_id == user.chat_id)
    )).all()
    # "Learned" means it has climbed past the short intervals — box 4 or 5 — not merely
    # that it was answered right once, which box 2 would count and which nobody would
    # recognise as knowing a word.
    learned = sum(1 for r in rows if r.box >= 4)
    return {
        "total": total,
        "started": len(rows),
        "learned": learned,
        "almost": sum(r.almost for r in rows),
    }


async def cards(
    session: AsyncSession,
    user: User,
    entitlement: Entitlement,
    now: datetime | None = None,
    size: int = VOCAB_ROUND_SIZE,
) -> dict:
    """A round for FLIP CARDS: the same selection, with the answer included.

    `round_for` withholds the answer on purpose — a typing test grades server-side so there
    is nothing in the payload to read ahead. A card is the opposite: revealing the answer IS
    the interaction, and it has to arrive in the same request or every flip costs a round
    trip on a phone that may be on a train.

    Same due-first ordering and the same alternating direction, so the two modes are the
    same study session seen two ways rather than two separate schedules.
    """
    base = await round_for(session, user, entitlement, now=now, size=size)
    lang = base["lang"]

    ids = [item["term_id"] for item in base["items"]]
    terms = {
        t.id: t for t in await session.scalars(
            select(VocabTerm).where(VocabTerm.id.in_(ids or [0]),
                                    visible_to(user.chat_id)))
    }
    for item in base["items"]:
        term = terms.get(item["term_id"])
        if term is None:
            continue
        item["answer"] = (
            term.gloss(lang) if item["direction"] == VOCAB_IT_TO_LANG else term.it)
    return base


async def recall(
    session: AsyncSession,
    user: User,
    term_id: int,
    knew: bool,
    entitlement: Entitlement,
    now: datetime | None = None,
) -> dict:
    """Move a term through the Leitner boxes from the learner's OWN verdict.

    Flashcards are self-graded, and that is a weaker signal than a typed answer: flipping a
    card and thinking "yes, I knew that" is recognition, and recognition is easier than
    recall. Leitner was invented for paper cards, so the scheme itself is right for this —
    but the two modes are not equally strong evidence and it is worth being clear that a
    box reached by tapping "I knew it" was reached more cheaply than one reached by typing.

    It is recorded as an event with the mode on it, so the split is measurable later if the
    vocabulary numbers ever start looking better than the learner does.

    `almost` is deliberately not touched. A card has two outcomes and "nearly" is not one of
    them; inventing a third from a two-way tap would put made-up data in the column the
    typing mode fills honestly.
    """
    _require_access(entitlement)
    now = now or datetime.now(timezone.utc)

    term = await _visible_term(session, user, term_id)

    row = await session.get(VocabProgress, (user.chat_id, term_id))
    if row is None:
        row = VocabProgress(chat_id=user.chat_id, term_id=term_id, box=1,
                            due_at=now, seen=0, wrong=0, almost=0)
        session.add(row)

    row.seen += 1
    if not knew:
        row.wrong += 1
    row.box, row.due_at = leitner.schedule(row.box, knew, now)
    row.last_answer_at = now

    await events.record(session, EV_VOCAB_RECALL, chat_id=user.chat_id,
                        term_id=term_id, knew=knew, box=row.box)

    return {"term_id": term.id, "box": row.box, "due_at": row.due_at, "knew": knew}
