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

   segnale (stradale)     RU: знак            EN: sign            (MAI "сигнал"/"signal")
   segnale luminoso       RU: светофор        EN: traffic light
   carreggiata            RU: проезжая часть  EN: carriageway
   corsia                 RU: полоса          EN: lane
   banchina               RU: обочина         EN: hard shoulder
   centro abitato         RU: населённый пункт EN: built-up area
   sorpasso               RU: обгон           EN: overtaking
   precedenza             RU: преимущество    EN: right of way
   arresto                RU: остановка (полная) EN: stopping
   fermata                RU: кратковременная остановка EN: brief stop
   sosta                  RU: стоянка         EN: parking
   autocarro              RU: грузовой автомобиль EN: goods vehicle
   autovettura            RU: легковой автомобиль EN: car
   ciclomotore            RU: мопед           EN: moped
   motociclo              RU: мотоцикл        EN: motorcycle

   `arresto`, `fermata` e `sosta` sono tre cose giuridicamente distinte in italiano:
   non usare la stessa parola per due di esse.

Rispondi SOLO con un oggetto JSON di questa forma esatta:

{"ru": {"stem": "...", "statement": "..."},
 "en": {"stem": "...", "statement": "..."}}

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
    try:
        try:
            response = await client.chat.completions.create(temperature=0, **kwargs)
        except Exception as exc:  # noqa: BLE001
            if "temperature" not in str(exc):
                raise
            response = await client.chat.completions.create(**kwargs)
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


async def ensure(
    session: AsyncSession, question: Question, lang: str
) -> Translation | None:
    """The translation for this question in this language, generating if nobody has yet."""
    row = await existing(session, question.id, lang)
    if row is not None:
        return row
    await generate(session, question)
    return await existing(session, question.id, lang)


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
            if await existing(session, question_id, TRANSLATION_LANGUAGES[0]) is not None:
                return
            await generate(session, question)
    except Exception:  # noqa: BLE001
        # A failed warm means the Italian only, for one question. Never a failed request.
        log.warning("warming translation for question %s failed", question_id, exc_info=True)


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
    if user.lang not in TRANSLATION_LANGUAGES:
        return {"translation_state": Access.OFF.value, "translation": None}, Access.OFF

    if not entitlement.can_translate:
        return {"translation_state": Access.LOCKED.value, "translation": None}, Access.LOCKED

    row = await ensure(session, question, user.lang)
    if row is None:
        return {"translation_state": Access.UNAVAILABLE.value, "translation": None}, \
            Access.UNAVAILABLE

    return {
        "translation_state": Access.SHOWN.value,
        "translation": {"lang": row.lang, "stem": row.stem, "statement": row.statement},
    }, Access.SHOWN
