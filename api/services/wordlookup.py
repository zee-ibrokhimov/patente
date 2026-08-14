"""Tap an Italian word in a question, get what it means, and keep it.

THE MEASUREMENT THAT SHAPED THIS

The shared glossary holds 1,104 curated exam words and covers only 14.5% of the word tokens
in the question bank. The words a learner is most likely to tap are precisely the ones
missing from it: `raffigurato` appears 2,796 times and is not there, nor are `veicolo`
(1,292), `veicoli` (965) or `velocità` (505). So a glossary lookup answers almost nothing and
nearly every tap would be a model call.

But the bank contains only 5,239 distinct words in total. Cached and SHARED, the first
learner to tap `raffigurato` pays for one small translation and every learner afterwards gets
it instantly. The ceiling is the whole bank translated once, ever — a bounded one-off, not a
cost that grows with the user base.

THE CACHE IS NOT THE GLOSSARY

`vocab_terms` with a NULL owner is a curated, frequency-ranked sheet the drill walks in
teaching order. Writing every tapped word into it would destroy the curation and the
ordering. The cache is its own table and the drill never reads it. What lands in the
learner's own vocabulary is a PERSONAL row, which the drill already knows how to schedule.

ALTERNATIVES ARE SEPARATED BY A COMMA, AND THAT IS NOT A STYLE CHOICE

The glossary already stores them that way — "звуковой сигнал, клаксон" — and
`vocab_grading.accepted_answers` splits on the comma so a learner typing EITHER is marked
correct. Store "схема или рисунок" instead and the drill accepts neither: verified, both
answers grade WRONG, because the whole phrase becomes the one expected string. So the model
is told to use a comma and told explicitly not to write "or" or "или".

KEYED ON THE DICTIONARY FORM

`veicolo` and `veicoli` are two tokens and one word. Keying on what was tapped would double
the cache and fill a learner's list with duplicates of the same noun, so the model is asked
for the lemma as well as the translation — which is what a dictionary does, and what a
stemmer gets wrong on Italian irregulars.

WHAT IT REFUSES TO LOOK UP

Nothing, except things that are not words. Function words like `per` and `che` are cheap,
are cached after the first tap, and a beginner may genuinely not know them — a stop-list
would be this module deciding what somebody is allowed to find confusing.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.models import Event, VocabTerm, WordForm, WordGloss
from api.services import events
from api.services.entitlement import Entitlement
from api.services.vocab import OWN_MAX_TERMS, VocabError, own_only
from shared.constants import EV_WORD_LOOKED_UP

log = logging.getLogger(__name__)

# Italian letters, plus the apostrophe that binds `dell'auto` and the hyphen in compounds.
# Everything else is punctuation and is trimmed off the ends of what the client sends.
WORD = re.compile(r"^[a-zàáèéìíòóùúâêîôûëïüç'\-]+$")

# Longest word in the bank is well under this; anything longer is not a tapped word.
MAX_WORD = 40

# Lookups a learner may make in a rolling day. Four times the streak goal of ten questions,
# so nobody reading carefully meets it, and it bounds what one account can spend on a cold
# cache — the only place in this feature where a person can cause a model call.
DAILY_LOOKUPS = 40

REASONING_EFFORT = "low"


class TooMany(Exception):
    """Past the daily lookup limit."""


def normalise(raw: str | None) -> str | None:
    """What was tapped, reduced to a word — or None if it was not one.

    Punctuation is stripped from the ENDS only: a tap lands on a token that may carry a
    comma or a full stop, and `dell'auto` must survive intact.
    """
    if not raw:
        return None
    text = unicodedata.normalize("NFC", raw).strip().lower()
    text = text.strip(".,;:!?()[]\"«»…“”‘’")
    if not text or len(text) > MAX_WORD:
        return None
    if not WORD.match(text):
        return None
    # A bare apostrophe or hyphen is punctuation wearing a word's clothes.
    if not any(c.isalpha() for c in text):
        return None
    return text


async def _used_today(session: AsyncSession, chat_id: int, now: datetime) -> int:
    """Lookups that reached the MODEL for this learner in the last rolling day.

    Counted from the append-only event log, for the reason pacing.py gives: a rolling window
    over events needs no column to reset, cannot drift out of step with what happened, and
    survives a learner deleting the word afterwards — which would otherwise be a way to buy
    unlimited translations by tidying up after each one.
    """
    since = now - timedelta(days=1)
    return await session.scalar(
        select(func.count()).select_from(Event)
        .where(Event.chat_id == chat_id, Event.type == EV_WORD_LOOKED_UP,
               Event.created_at >= since)
    ) or 0


async def cached(session: AsyncSession, form: str) -> WordGloss | None:
    """The gloss for a tapped FORM, through the surface-form index.

    Looked up by what was tapped, not by the lemma. Keying only on the lemma is what made an
    early version miss on every inflected word — `raffigurato` never found `raffigurare`, so
    the second learner to tap it paid again, with identical correct text and a wrong bill.
    """
    row = await session.get(WordForm, form)
    if row is not None:
        return await session.get(WordGloss, row.lemma)
    # A form that IS its own lemma — most nouns as tapped in the singular — needs no index
    # entry to be found, and this is the common case.
    return await session.get(WordGloss, form)


async def translate(word: str) -> dict | None:
    """Ask the model for the dictionary form and the three glosses. None if it cannot.

    Returns None rather than raising: a word this fails on is a word the learner does not
    get, and that must not take the question they were reading down with it.
    """
    from shared.config import settings

    if not settings.openai_api_key:
        return None

    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=settings.openai_api_key)
    kwargs = dict(
        model=settings.openai_translate_model,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": (
                "You are a dictionary for Italian driving-theory vocabulary. "
                "Given one Italian word as it appeared in a sentence, reply with JSON: "
                '{"lemma": "<dictionary form, lowercase>", "en": "...", "ru": "...", '
                '"uz": "..."}. '
                "The lemma is the form a dictionary would list: singular for nouns, "
                "masculine singular for adjectives, infinitive for verbs. "
                "Give the common renderings, SEPARATED BY A COMMA, most usual first — "
                "for example figura -> \"схема, рисунок\". One is fine when the word "
                "really has one; never more than three. "
                "No explanation, no article, no punctuation at the end, and never the word "
                "\"or\" or \"или\" between them: the comma IS the separator and anything "
                "else is read as part of the answer. "
                "Where the word has a specific meaning in road traffic, give that meaning "
                "first. Uzbek uses the Latin alphabet."
            )},
            {"role": "user", "content": word},
        ],
    )
    # The same ladder translations.py uses, and for the same reason recorded there: a single
    # fallback to no parameters is how `reasoning_effort` was silently dropped on every call,
    # at five to ten times the cost, with the constant set and the tests passing.
    attempts = (
        dict(temperature=0, reasoning_effort=REASONING_EFFORT),
        dict(reasoning_effort=REASONING_EFFORT),
        dict(temperature=0),
        dict(),
    )
    try:
        response = None
        last: Exception | None = None
        for extra in attempts:
            try:
                response = await client.chat.completions.create(**extra, **kwargs)
                break
            except Exception as exc:  # noqa: BLE001
                text = str(exc)
                if "temperature" not in text and "reasoning_effort" not in text:
                    raise
                last = exc
        if response is None:
            raise last or RuntimeError("no usable parameter combination")
        parsed = json.loads(response.choices[0].message.content)
    except Exception as exc:  # noqa: BLE001
        log.error("word lookup failed for %r: %s %s", word, type(exc).__name__, exc)
        return None

    lemma = normalise(str(parsed.get("lemma") or "")) or word
    glosses = {lang: str(parsed.get(lang) or "").strip()[:120] for lang in ("en", "ru", "uz")}
    if not all(glosses.values()):
        log.warning("word lookup for %r came back incomplete: %s", word, glosses)
        return None
    return {"lemma": lemma, **glosses}


async def look_up(
    session: AsyncSession, user, raw: str, entitlement: Entitlement,
    now: datetime | None = None,
) -> VocabTerm:
    """Translate a tapped word, cache it, and put it in this learner's vocabulary.

    Returns the learner's own term, so the caller can show the meaning and offer to undo.
    Raises VocabError for anything the learner can act on, and TooMany past the daily limit.
    """
    now = now or datetime.now(timezone.utc)
    word = normalise(raw)
    if word is None:
        raise VocabError(422, "that is not a word")

    from api.services.vocab import _require_access

    _require_access(entitlement)

    known = await cached(session, word)
    if known is None:
        # Only a MISS costs anything, so the limit is charged here and not on every tap.
        if await _used_today(session, user.chat_id, now) >= DAILY_LOOKUPS:
            raise TooMany("that is as many new words as one day can hold")
        result = await translate(word)
        if result is None:
            raise VocabError(503, "could not look that word up")
        known = await session.get(WordGloss, result["lemma"])
        if known is None:
            known = WordGloss(lemma=result["lemma"], en=result["en"],
                              ru=result["ru"], uz=result["uz"], created_at=now)
            session.add(known)
            await session.flush()
        # Remember the form that was tapped, so the NEXT learner to tap it hits the cache.
        # Without this the cache is only ever reachable from words that happen to equal
        # their own dictionary form.
        if word != known.lemma and await session.get(WordForm, word) is None:
            session.add(WordForm(form=word, lemma=known.lemma, created_at=now))
            await session.flush()
        # Recorded on the MISS only, which is what the limit is about and what the content
        # feedback is about: a cache hit costs nothing and says nothing new.
        await events.record(session, EV_WORD_LOOKED_UP, chat_id=user.chat_id,
                            word=word, lemma=known.lemma)

    # Already in their list — the same word tapped twice, or met in another question. Handed
    # back rather than refused: the learner tapped it to find out what it means, and an error
    # is a worse answer to that than the meaning they already had.
    mine = await session.scalar(
        select(VocabTerm).where(own_only(user.chat_id),
                                func.lower(VocabTerm.it) == known.lemma))
    if mine is not None:
        return mine

    held = await session.scalar(
        select(func.count()).select_from(VocabTerm).where(own_only(user.chat_id))) or 0
    if held >= OWN_MAX_TERMS:
        raise VocabError(422, "that is as many words as one list can hold")

    # Real per-language glosses, unlike `add_own` — which writes one string the learner typed
    # into all three columns, because there it IS one note in one language. Here there are
    # three genuine translations, so switching the interface language shows the right one.
    term = VocabTerm(owner_chat_id=user.chat_id, rank=None, it=known.lemma,
                     en=known.en, ru=known.ru, uz=known.uz)
    session.add(term)
    await session.flush()
    return term
