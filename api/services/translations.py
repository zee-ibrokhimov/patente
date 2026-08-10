"""Translating a ministerial question, on request.

Plan §3.3 had `translate.py` render the *approved* Italian explanation into RU and EN as
an offline pass. That pass is not being written (STATUS.md §13). This does the other and
more important half: the **question** itself, which is what a Russian or English speaker
actually needs to sit the exam, and the reason they would choose this over a free Italian
quiz app.

Both languages come back in one call and are cached in `translations`, whose
`(question_id, lang)` is already unique — no schema change.

A TRANSLATION IS NOT AN EXPLANATION
-----------------------------------
The stored Italian is the thing being learned: candidates train to recognise the exact
ministerial phrasing, and the translation rides underneath as a comprehension aid, never
as a replacement (`Translation` in api/models/content.py). So the prompt asks for a
literal rendering that keeps the legal register — not a clearer, friendlier, or
explanatory one. "Il segnale raffigurato" must come back as "the sign shown", not as the
name of whatever sign the model imagines it to be: guessing the sign is the failure that
cost two of three clusters when it was tried for explanations, and here it would put a
wrong answer directly under the question.

THE LATENCY PROBLEM IS THE WHOLE PROBLEM
----------------------------------------
Explanations had somewhere to hide a few seconds: the user reads the statement and
answers before wanting one. Translations belong to the moment the question appears, which
is every single interaction, and nobody waits three seconds per question. Two things
together fix that, neither sufficient alone:

  · `warm` runs in the background when a question is served, so the *next* reader of that
    question — and there will be many, across 7106 questions and a Leitner schedule that
    deliberately repeats them — finds it cached.
  · the client sends the Italian immediately and edits the message when the translation
    lands, so even a cold question is readable at once.

Cost is small: a question is ~150 characters in and ~300 out, so the whole bank is a few
euros rather than the ~€75 the explanations cost.
"""

from __future__ import annotations

import asyncio
import time

import json
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.models import Question, Translation
from api.services.entitlement import Access, Entitlement
from api.services.explanations import is_fatal, openai_client
from shared.config import settings
from shared.constants import TRANSLATION_LANGUAGES
from shared.db import async_session_factory

log = logging.getLogger(__name__)

# How hard the model should THINK about a translation. Measured on gpt-5-mini, five real
# ministerial statements, Italian to Russian:
#
#     default   4.2 - 6.9s
#     low       2.8 - 4.2s      same text, or better
#     minimal   1.7s            terser, and starts to paraphrase
#
# On "veicoli a trazione animale" the low-effort answer was the more precise one — it used
# "гужевого транспорта", the actual term, where the default produced a generic phrase. On
# "il segnale raffigurato", the phrase this project's notes warn was once mangled by a
# cheaper setting, low and default were IDENTICAL across three statements. That warning was
# written against a different model and no longer describes this one.
#
# It matters because it is the whole loading screen. A cold five-question window was
# measured at the 75-second deadline with four of ten jobs unfinished — which is what
# "in question 4 there was no translation and i waited again" looks like from the outside.
# Twelve seconds of reasoning about a sentence with no ambiguity in it was most of that.
REASONING_EFFORT = "low"

SYSTEM_PROMPT = """\
Traduci una domanda del listato ufficiale dei quiz per la patente di guida italiana.

REGOLE, in ordine di importanza:

1. TRADUZIONE LETTERALE. Il candidato deve imparare a riconoscere la formulazione
   ministeriale esatta: la traduzione gli serve solo per capire l'italiano, non per
   sostituirlo. Non semplificare, non chiarire, non spiegare, non aggiungere nulla.

2. MANTIENI IL REGISTRO GIURIDICO. "È vietato", "obbligo", "deve", "consente" hanno un
   valore preciso: rendili con il termine equivalente, non con un sinonimo più comune.

3. NON INDOVINARE IL SEGNALE. Se l'affermazione dice "il segnale raffigurato" o "il
   segnale in figura", traduci esattamente questo. Non hai l'immagine e non devi dedurre
   di quale segnale si tratti: scriverne il nome metterebbe una risposta sbagliata
   direttamente sotto la domanda.

4. Conserva la struttura: se c'è una premessa (stem) e un'affermazione, restano due
   campi distinti.

5. USA QUESTI TERMINI. Sono i termini che ricorrono in migliaia di domande e che una
   traduzione generica sbaglia:

   segnale (stradale)     RU: знак            EN: sign            UZ: yo'l belgisi
                                                                  (MAI "сигнал"/"signal"/"signal")
   segnale luminoso       RU: светофор        EN: traffic light   UZ: svetofor
   carreggiata            RU: проезжая часть  EN: carriageway     UZ: qatnov qismi
   corsia                 RU: полоса          EN: lane            UZ: yo'lak
   banchina               RU: обочина         EN: hard shoulder   UZ: yo'l cheti
   centro abitato         RU: населённый пункт EN: built-up area  UZ: aholi punkti
   sorpasso               RU: обгон           EN: overtaking      UZ: quvib o'tish
   precedenza             RU: преимущество    EN: right of way    UZ: ustunlik
   arresto                RU: остановка (полная) EN: stopping     UZ: to'xtash
   fermata                RU: кратковременная остановка EN: brief stop UZ: qisqa to'xtash
   sosta                  RU: стоянка         EN: parking         UZ: turish (parking)
   autocarro              RU: грузовой автомобиль EN: goods vehicle UZ: yuk avtomobili
   autovettura            RU: легковой автомобиль EN: car          UZ: yengil avtomobil
   ciclomotore            RU: мопед           EN: moped           UZ: moped
   motociclo              RU: мотоцикл        EN: motorcycle      UZ: mototsikl

   `arresto`, `fermata` e `sosta` sono tre cose giuridicamente distinte in italiano:
   non usare la stessa parola per due di esse. Lo stesso vale in uzbeco.

6. I VERBI DEI SEGNALI SONO TRE E NON VANNO MAI COLLASSATI.

   preannuncia  = avvisa di cio che si trovera PIU AVANTI (segnali di pericolo)
                  RU: предупреждает о   EN: gives advance warning of   UZ: oldindan ogohlantiradi
   preavvisa    = annuncia in anticipo una prescrizione o una direzione, spesso con
                  la distanza (segnali di preavviso)
                  RU: заранее извещает о EN: gives advance notice of   UZ: oldindan xabar beradi
   indica       = dice che cosa C'E, qui (segnali di indicazione)
                  RU: указывает          EN: indicates                 UZ: ko'rsatadi

   NEL LISTATO ESISTONO COPPIE IDENTICHE TRANNE IL VERBO, CON RISPOSTA OPPOSTA:
     "Il segnale raffigurato preannuncia una curva pericolosa a destra"   -> VERO
     "Il segnale raffigurato indica una curva pericolosa a destra"        -> FALSO
     "Il segnale raffigurato preannuncia l'obbligo di svoltare a destra"  -> FALSO
     "Il segnale raffigurato indica l'obbligo di svoltare a destra"       -> VERO

   Se traduci due di questi verbi con la stessa parola, le due frasi diventano IDENTICHE
   nella lingua di arrivo e lo studente vede due frasi uguali con risposte opposte: non
   puo impararle, e l'app sembra rotta. MAI rendere `preannuncia` con
   "indica"/"указывает"/"indicates". MAI inventare calchi come "preannounces".

7. UZBECO IN ALFABETO LATINO, mai in cirillico. È la grafia ufficiale in Uzbekistan.
   Usa l'apostrofo modificatore corretto: o' e g' (o'tish, to'xtash, g'ildirak).

Rispondi SOLO con un oggetto JSON di questa forma esatta:

{"ru": {"stem": "...", "statement": "..."},
 "en": {"stem": "...", "statement": "..."},
 "uz": {"stem": "...", "statement": "..."}}

Se non c'è una premessa, "stem" è null.
"""


def build_messages(question: Question) -> list[dict]:
    payload = {
        "stem": question.stem_it,
        "statement": question.statement_it,
    }
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def parsed_translations(parsed: dict) -> dict[str, dict]:
    """{lang: {stem, statement}}, keeping only languages we serve and non-empty text."""
    out: dict[str, dict] = {}
    for code in TRANSLATION_LANGUAGES:
        entry = parsed.get(code)
        if not isinstance(entry, dict):
            continue
        statement = entry.get("statement")
        if not isinstance(statement, str) or not statement.strip():
            continue
        stem = entry.get("stem")
        out[code] = {
            "stem": stem.strip() if isinstance(stem, str) and stem.strip() else None,
            "statement": statement.strip(),
        }
    return out


async def existing(session: AsyncSession, question_id: int, lang: str) -> Translation | None:
    return await session.scalar(
        select(Translation).where(
            Translation.question_id == question_id, Translation.lang == lang
        )
    )


async def generate(session: AsyncSession, question: Question, model: str | None = None) -> bool:
    """Translate one question into every served language. True if anything was stored.

    Never raises for an API problem — the caller's job is to show the Italian either way.
    """
    if not settings.openai_api_key:
        log.error("OPENAI_API_KEY is not set — translations cannot be generated")
        return False

    client = openai_client()
    kwargs = dict(
        model=model or settings.translate_model,
        messages=build_messages(question),
        response_format={"type": "json_object"},
    )
    # Degrade one parameter at a time, most-wanted last.
    #
    # This was a single fallback to NO parameters, and it silently undid the thing it was
    # meant to protect. gpt-5-mini rejects `temperature=0` outright — "does not support 0
    # with this model" — so every call took the fallback and dropped `reasoning_effort`
    # along with it. The constant was set, the tests passed, and production never once ran
    # a low-effort translation: measured at 13.8-36.7s each where the same model answers a
    # low-effort request in 2.8-4.2s.
    #
    # So reasoning_effort is dropped LAST, and only if it is the thing being complained
    # about. temperature is the expendable one: it pins determinism, which is nice to have,
    # while effort is most of the wall-clock the learner sits through.
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
        log.error(
            "translation failed for question %s: %s %s%s",
            question.id, type(exc).__name__, exc,
            " (fatal — the feature is down, not this question)" if is_fatal(exc) else "",
        )
        return False

    texts = parsed_translations(parsed)
    if not texts:
        log.warning("translation of question %s came back empty", question.id)
        return False

    for code, body in texts.items():
        row = await existing(session, question.id, code)
        if row is None:
            session.add(Translation(question_id=question.id, lang=code, **body))
        elif row.reviewed_at is None:
            # A reviewed translation is not replaced by a fresh guess at it.
            row.stem, row.statement = body["stem"], body["statement"]
    await session.commit()
    log.info("translated question %s into %s", question.id, ",".join(sorted(texts)))
    return True


# One lock per QUESTION, not per (question, language): a single call produces all three,
# so a Russian and an Uzbek reader arriving together should wait on the same call rather
# than make two. Explanations have had this since they were written; translations did not,
# and the first question of every session is requested by the client at the same moment it
# is warmed in the background — two paid calls for one result, on every session start.
_locks: dict[int, asyncio.Lock] = {}

# Languages a question has been asked for and did not come back with, and when.
#
# Without this, a question the model will not produce Uzbek for regenerates on EVERY
# request from an Uzbek reader — a paid call each time, forever, for a row that never
# appears. In-process and deliberately not persisted: a restart is a fine moment to try
# again, and the alternative is a schema for remembering failures.
_missing: dict[tuple[int, str], float] = {}
RETRY_AFTER = 3600.0


def _recently_failed(question_id: int, lang: str, now: float | None = None) -> bool:
    when = _missing.get((question_id, lang))
    if when is None:
        return False
    now = now if now is not None else time.monotonic()
    if now - when > RETRY_AFTER:
        _missing.pop((question_id, lang), None)
        return False
    return True


async def ensure(
    session: AsyncSession, question: Question, lang: str
) -> Translation | None:
    """The translation for this question in this language, generating if nobody has yet.

    Keyed on the REQUESTED language, so adding a language to TRANSLATION_LANGUAGES
    backfills correctly: a question cached in ru/en before Uzbek existed misses on uz,
    regenerates, and the new call writes all three. Existing unreviewed rows are rewritten
    in place by that call, which is why a human-reviewed row is spared (see `generate`).
    """
    row = await existing(session, question.id, lang)
    if row is not None:
        return row
    if _recently_failed(question.id, lang):
        return None

    lock = _locks.setdefault(question.id, asyncio.Lock())
    async with lock:
        # Re-check inside the lock: whoever held it may have just written the row.
        row = await existing(session, question.id, lang)
        if row is not None:
            return row
        await generate(session, question)
        row = await existing(session, question.id, lang)

    if row is None:
        # Asked for, generated, and this language still did not appear. Remember, so the
        # next reader is not charged for the same disappointment.
        _missing[(question.id, lang)] = time.monotonic()
        log.warning("question %s produced no %s translation — not retrying for an hour",
                    question.id, lang)
    return row


async def warm(question_id: int) -> None:
    """Translate ahead of the next reader, on its own session.

    Called as a background task at question-serve time. It will not help the user who
    triggered it — their client edits the message instead — but 7106 questions on a
    Leitner schedule are served repeatedly and to more than one person, so most reads are
    cache hits after the first.
    """
    try:
        factory = async_session_factory()
        async with factory() as session:
            question = await session.get(Question, question_id)
            if question is None:
                return
            # Every language must be present, not just the first. This used to check
            # TRANSLATION_LANGUAGES[0] as a stand-in for "fully cached", which was true
            # only while every language was written by the same call. The day a new
            # language is added, every question that already has Russian short-circuits
            # here and is NEVER warmed for the new one — so every reader of it pays the
            # ~3s foreground latency this whole module exists to avoid.
            for code in TRANSLATION_LANGUAGES:
                if await existing(session, question_id, code) is None:
                    break
            else:
                return
            await generate(session, question)
    except Exception:  # noqa: BLE001
        # A failed warm means the Italian only, for one question. Never a failed request.
        log.warning("warming translation for question %s failed", question_id, exc_info=True)


def reading_language(user) -> str:
    """The language this learner wants QUESTIONS in.

    `translation_lang` when they have chosen one, otherwise the interface language — which
    is what the product did when the two were a single field, and what every row created
    before the column existed still means.

    One function rather than `user.translation_lang or user.lang` at each call site, because
    there are four of them and the paywall has to agree with the payload about which language
    is on offer: `entitlement.translation_offer` refuses a language we do not translate into,
    and if the two computed that language differently a learner would be shown a locked strip
    for a translation the API would happily have served, or vice versa.
    """
    return user.translation_lang or user.lang


async def deliver(
    session: AsyncSession, question: Question, user, entitlement: Entitlement
) -> tuple[dict, Access]:
    """Hand over the translation, or say why not.

    Translations are a paid feature (§4.3) and `translations_on` is the user's own switch,
    so both are checked before any API call. Unlike an explanation nothing is spent per
    view — there is no taster to burn and no conversion event, because the paywall for
    translations is met at the question, which `question_payload` already reports.
    """
    if not user.translations_on:
        return {"translation_state": Access.OFF.value, "translation": None}, Access.OFF

    # A user reading in a language we do not translate INTO has nothing to fetch, and
    # asking for one costs real money every single time. `ensure` would miss the cache,
    # pay for a generation, and then `parsed_translations` would drop the result because
    # the language is not in TRANSLATION_LANGUAGES — so nothing is ever written and the
    # next view of the same question pays again. Unbounded, and invisible.
    #
    # Italian is the live case: it is a UI language but not a translation target, which
    # is correct (the question is already Italian). entitlement.translation_offer has
    # always had this guard; deliver did not, and the two disagreed.
    wanted = reading_language(user)
    if wanted not in TRANSLATION_LANGUAGES:
        return {"translation_state": Access.OFF.value, "translation": None}, Access.OFF

    if not entitlement.can_translate:
        return {"translation_state": Access.LOCKED.value, "translation": None}, Access.LOCKED

    row = await ensure(session, question, wanted)
    if row is None:
        return {"translation_state": Access.UNAVAILABLE.value, "translation": None}, \
            Access.UNAVAILABLE

    return {
        "translation_state": Access.SHOWN.value,
        "translation": {"lang": row.lang, "stem": row.stem, "statement": row.statement},
    }, Access.SHOWN
