"""Producing the explanation for a rule cluster, on request.

Plan §3.3 said generate once offline and never at runtime. That was reversed on
29 July: the 169 hours of human review it implied were about to become the launch
gate for an otherwise finished product, and generating on demand bills only for the
clusters someone actually reaches while making review priority demand-ordered instead
of guessed. STATUS.md §13 is the decision and its costs.

This module is the whole of that logic, and it lives under `api/` because the API owns
all business logic — `bot/` and `webapp/` must not contain a second definition of what
a servable explanation is. `content/generate.py` is now a batch caller of the same
functions, useful for pre-warming a topic before a launch.

THE ANSWER KEY IS A TEST, NOT AN INPUT
--------------------------------------
The obvious prompt hands the model the statement, the correct answer and the article,
and asks it to justify the answer. That prompt cannot fail: whatever the key says the
model will produce fluent Italian agreeing with it, including when the key is wrong
because extraction slipped a row.

So the key is withheld. The model decides VERO/FALSO from the article text and the
figure alone, and the stored answer is compared afterwards. A disagreement means one of
three things — the model reasoned badly, the topic is mapped to the wrong article, or
the ministerial answer in our database is wrong — and only a human can say which, so it
flags.

THE MODEL SEES THE FIGURE
-------------------------
Most sign statements say only "il segnale raffigurato". A text-only model cannot
resolve that at all, and guessing the sign from the statement wording was measured
wrong on 2 of 3 clusters, with the model dutifully following the wrong guess into
confidently wrong explanations (STATUS.md §12). The figure is the ground truth, all 409
are on disk, so the image goes in the request.

The image belongs to the cluster as much as to the question: under the figure strategy
every member of a figure cluster shares one `image_path` — the figure *is* the cluster
key — so caching per cluster stays correct.

ALL THREE LANGUAGES COME BACK IN ONE CALL
----------------------------------------
Plan §3.3 had a separate pass translating the *approved* Italian into RU and EN, so that
legal substance was reviewed once and translation fidelity separately. That pass is not
being written (STATUS.md §13): the same request returns Italian, Russian and English,
and three rows are stored.

The Italian is still the canonical one — the gates run on it, and it is what a reviewer
reads — but a Russian speaker gets Russian on the first ask rather than waiting for a
translation pass that may never happen. The expensive half of the call is the statute in
the prompt, which is identical either way; three explanations instead of one costs a few
hundred extra output tokens.

Numeric and citation gates deliberately run on the Italian only. The claim is the same
claim in every language, so a status derived from one applies to all; matching "км/ч" as
well as "km/h" would be three regexes agreeing with each other by hand.

WHAT REACHES A USER
-------------------
`draft` is served, `flagged` is withheld. Anything that tripped a cluster-level gate
reads as `Access.UNAVAILABLE`, which entitlement already distinguishes from "pay for
it", and a human upgrades it to `approved` through the step-7 review loop.

Disagreeing with the answer key is handled **per statement**, not per cluster. Measured
on *Segnali di precedenza*, 8 of 12 clusters disagreed on 1-3 statements out of 12, and
in every case the explanation of the rule was correct — the disputes were about derived
facts the article does not state. Withholding whole clusters for that left 5 of 15
servable. So the disputed question ids are recorded and the explanation is withheld only
for those, which keeps the safety property that matters — a user never sees an
explanation contradicting the answer they were just shown — without suppressing the ten
statements it explains perfectly well.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.models import Explanation, Question, Topic
from api.services import events
from api.services.articles import (
    LABEL,
    articles_for,
    cite,
    load_corpus,
    sign_index,
    signs_in,
)
from api.services.entitlement import Access, Entitlement
from shared.config import CONTENT_OUT, settings
from shared.constants import (
    EV_EXPLANATION_VIEWED,
    EV_PAYWALL_HIT,
    LANG_IT,
    SERVABLE_STATUSES,
    STATUS_APPROVED,
    STATUS_DRAFT,
    STATUS_FLAGGED,
    EXPLANATION_FALLBACK,
    EXPLANATION_LANGUAGES,
)
from shared.db import async_session_factory

log = logging.getLogger(__name__)

# How much statute goes in front of the model. Measured, and counter-intuitive:
# raising this to 60000 so that every article of a topic fits made *all three* test
# clusters decline as "articles insufficient", against two of three answering at
# 24000. Burying the governing article among its neighbours makes the model less able
# to decide, not more. Do not raise it without measuring again.
CONTEXT_CHARS = 24_000

# Statements judged per cluster. A 34-member cluster does not need all 34 checked to
# catch a bad explanation.
MAX_STATEMENTS = 12

NUMERIC_RE = re.compile(
    r"\d|\b(?:km/h|m/s|g/l|km|metri|metro|tonnellate|anni|anno|mesi|mese|"
    r"giorni|per\s?cento|euro)\b",
    re.I,
)

# Article and figure references are citations, not claims — and the prompt *requires* a
# citation, so every draft contains one. Left in, they matched the `\d` above and the
# numeric gate fired on 100% of drafts, which is a gate the reviewer learns to ignore.
CITATION_RE = re.compile(
    r"art(?:icolo)?\.?\s*\d+[a-z\-]*(?:\s*,?\s*comma\s*\d+[a-z\-]*)?"
    r"|fig(?:ura)?\.?\s*[IVX]+\.\s*\d+[a-z]?",
    re.I,
)

SYSTEM_PROMPT = """\
Sei un esperto di diritto della circolazione stradale italiano.

Ti vengono forniti il testo integrale di uno o più articoli del Codice della Strada
o del suo Regolamento di esecuzione, alcune affermazioni tratte dal listato ufficiale
dei quiz per la patente di guida e, quando esiste, l'immagine del segnale a cui le
affermazioni si riferiscono.

Svolgi i compiti in questo ordine:

1. Se c'è un'immagine, guardala e stabilisci quale segnale rappresenta. Le affermazioni
   che dicono "il segnale raffigurato" o "il segnale in figura" si riferiscono a quel
   segnale.

2. Per OGNI affermazione, decidi se è VERA o FALSA basandoti sugli articoli forniti e
   sull'immagine. Non ti viene detto quale sia la risposta corretta: devi ricavarla tu.

3. Scrivi una spiegazione in italiano di DUE frasi, al massimo tre, che esponga la
   REGOLA che sta sotto a queste affermazioni, e che citi l'articolo fra parentesi
   (per esempio "(art. 148 C.d.S.)"). Spiega la regola, non le singole affermazioni:
   la stessa spiegazione deve servire per tutte.

4. Fornisci la STESSA spiegazione anche in russo e in inglese. Non è una traduzione
   letterale da rivedere a parte: è la stessa regola espressa nelle tre lingue. I
   riferimenti agli articoli restano nella forma italiana ("art. 148 C.d.S.") in tutte
   le lingue, perché è così che il candidato li troverà nel codice.

Se gli articoli forniti non bastano a decidere, imposta "insufficiente": true e lascia
"spiegazione" vuota. NON inventare e NON tirare a indovinare: una spiegazione assente è
accettabile, una spiegazione sbagliata non lo è.

Rispondi SOLO con un oggetto JSON di questa forma esatta:

{"insufficiente": false,
 "spiegazione": {"it": "...", "ru": "...", "en": "..."},
 "articolo_citato": "art. 148 C.d.S.",
 "segnale_riconosciuto": "DARE PRECEDENZA",
 "verdetti": [{"n": 1, "risposta": "VERO", "certezza": "alta"}]}

"risposta" è "VERO" o "FALSO". "certezza" è "alta", "media" o "bassa".
"segnale_riconosciuto" è il nome del segnale nell'immagine, o "" se non c'è immagine.
Includi un verdetto per ogni affermazione, con "n" pari al suo numero.
"""

# One lock per cluster. Ten users answering the same question at once would otherwise
# be ten identical paid calls, and the loser of the race would overwrite the winner.
_locks: dict[int, asyncio.Lock] = {}

# The corpus is ~1.5M characters of JSON and never changes at runtime.
_corpus: dict | None = None
_index: dict | None = None


def openai_client():
    """The client, behind a function so a test can replace it.

    Imported lazily so that importing this module — which the API does at startup —
    does not depend on the SDK being installed, and so the substitution point is a
    module attribute rather than an import inside a function body.
    """
    from openai import AsyncOpenAI

    return AsyncOpenAI(api_key=settings.openai_api_key)


def corpus_and_index() -> tuple[dict, dict]:
    global _corpus, _index
    if _corpus is None:
        _corpus = load_corpus()
        _index = sign_index(_corpus)
    return _corpus, _index


# --- the pure parts, shared with the batch caller and directly testable -------

def sample_statements(members: list[dict], cap: int = MAX_STATEMENTS) -> list[dict]:
    """Up to `cap` statements, keeping both answers represented.

    Judging only the VERO ones would miss exactly the case this gate exists for: a
    model that agrees with whatever it is shown.
    """
    ordered = sorted(members, key=lambda m: m["id"])
    if len(ordered) <= cap:
        return ordered
    true_ = [m for m in ordered if m["answer"]]
    false_ = [m for m in ordered if not m["answer"]]
    out: list[dict] = []
    while len(out) < cap and (true_ or false_):
        for pool in (true_, false_):
            if pool and len(out) < cap:
                out.append(pool.pop(0))
    return sorted(out, key=lambda m: m["id"])


def select_articles(
    topic_name: str,
    statements: list[str],
    corpus: dict,
    index: dict,
    budget: int = CONTEXT_CHARS,
) -> list[dict]:
    """The articles to put in front of the model, most specific first.

    Sign-name matches come first because they name the article defining the sign in
    question; the topic's hand-mapped articles follow as the floor, so no cluster is
    ever sent with no statute at all. Matches are used only for *ordering* — a mention
    is not an identification, and being wrong here costs some irrelevant text rather
    than a wrong answer.
    """
    ordered: list[tuple[str, str]] = []
    for reference in signs_in(statements, index) + articles_for(topic_name):
        if reference not in ordered:
            ordered.append(reference)

    chosen, used = [], 0
    for source, number in ordered:
        article = corpus[source].get(number)
        if article is None:          # repealed, or never fetched
            continue
        if used and used + len(article["text"]) > budget:
            continue
        chosen.append({"source": source, "number": number, **article})
        used += len(article["text"])
    return chosen


def build_text_prompt(topic: str, judged: list[dict], grounded: list[dict]) -> str:
    parts = [f"ARGOMENTO MINISTERIALE: {topic}", "", "ARTICOLI DI LEGGE:"]
    for article in grounded:
        rubric = f" — {article['rubric']}" if article["rubric"] else ""
        parts += [
            "",
            f"[{LABEL[article['source']]}] {cite(article['source'], article['number'])}{rubric}",
            article["text"],
        ]
    parts += ["", "AFFERMAZIONI DA VALUTARE:"]
    parts += [f"{i}. {m['statement']}" for i, m in enumerate(judged, 1)]
    return "\n".join(parts)


def figure_part(image_path: str | None) -> dict | None:
    """The figure as a data URL, or None.

    Inlined rather than served by URL: these are 5 KB sign images already on disk, and
    a public URL for them is a deployment concern that buys nothing here.
    """
    if not image_path:
        return None
    path = CONTENT_OUT / image_path
    if not path.exists():
        log.warning("figure %s referenced by a question is not on disk", image_path)
        return None
    mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{mime};base64,{encoded}"},
    }


def build_messages(
    topic: str, judged: list[dict], grounded: list[dict], image_path: str | None
) -> list[dict]:
    content: list[dict] = [{"type": "text", "text": build_text_prompt(topic, judged, grounded)}]
    figure = figure_part(image_path)
    if figure:
        content.append(figure)
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": content},
    ]


def parsed_texts(parsed: dict) -> dict[str, str]:
    """{lang: explanation} from the model's reply, keeping only languages we serve.

    Tolerates a bare string where the schema asks for an object: a model that answers
    with one explanation instead of three has still answered, and the Italian is the
    part everything else depends on.
    """
    raw = parsed.get("spiegazione")
    if isinstance(raw, str):
        raw = {LANG_IT: raw}
    if not isinstance(raw, dict):
        return {}
    return {
        code: value.strip()
        for code, value in raw.items()
        if code in EXPLANATION_LANGUAGES and isinstance(value, str) and value.strip()
    }


def check_gates(parsed: dict, judged: list[dict]) -> tuple[str, list[str], list[dict]]:
    """(status, reasons, disagreements).

    `reasons` withhold the whole cluster and make the status `flagged`. `disagreements`
    do not: they withhold the individual statements they concern, and are recorded on the
    row for the reviewer.

    That split is measured, not assumed. On *Segnali di precedenza*, 8 of 12 clusters
    disagreed with the answer key on 1-3 statements out of 12, and in every case the
    explanation of the rule was right — the disputed statements were about derived facts
    the article does not state ("si trova sulle corsie di accelerazione", "perde efficacia
    in presenza di agente"). Treating that as a cluster-level failure withheld two thirds
    of the topic to protect against something that had not gone wrong.

    A number is different and stays cluster-level: a wrong speed limit is the worst thing
    this product can say, and it can be wrong anywhere in the sentence.
    """
    reasons: list[str] = []
    verdicts = {
        v.get("n"): str(v.get("risposta", "")).upper()
        for v in parsed.get("verdetti", [])
        if isinstance(v, dict)
    }

    disagreements = []
    for i, member in enumerate(judged, 1):
        said = verdicts.get(i)
        if said not in ("VERO", "FALSO"):
            reasons.append(f"no verdict for statement {i}")
            continue
        if (said == "VERO") != bool(member["answer"]):
            disagreements.append({
                "question_id": member["id"],
                "stored": "VERO" if member["answer"] else "FALSO",
                "model": said,
                "statement": member["statement"],
            })

    # Deliberately NOT appended to `reasons`: this withholds the disputed statements,
    # not the cluster. `disputed_ids` is what the serving path checks.
    #
    # One exception. Disagreeing about *most* of the cluster is no longer a quibble about
    # a peripheral fact — it means the model and the answer key disagree about the rule
    # itself, and there is no sound explanation to salvage.
    if len(disagreements) > len(judged) / 2:
        reasons.append(
            f"argues against the stored answer on {len(disagreements)}/{len(judged)} "
            f"statements — most of the cluster"
        )

    # The Italian only. The claim is the same claim in every language, so a status
    # derived from one applies to all — and matching "км/ч" as well as "km/h" would be
    # three regexes kept in agreement by hand.
    italian = parsed_texts(parsed).get(LANG_IT, "")
    if NUMERIC_RE.search(CITATION_RE.sub("", italian)):
        reasons.append("contains a number or a unit")
    if any(
        str(v.get("certezza", "")).lower() == "bassa"
        for v in parsed.get("verdetti", [])
        if isinstance(v, dict)
    ):
        reasons.append("model reports low confidence")

    return (STATUS_FLAGGED if reasons else STATUS_DRAFT), reasons, disagreements


def record_flags(reasons: list[str], disagreements: list[dict]) -> str | None:
    """Why the reviewer should look, in the form they need: which statement, and what
    each side said. Recorded even when it does not withhold anything — a disagreement is
    still the single best pointer to either a bad explanation or a bad answer key."""
    if disagreements and not any("argues against" in r for r in reasons):
        reasons = reasons + [
            f"disputes the stored answer on {len(disagreements)} statement(s)"
        ]
    return "; ".join(
        reasons
        + [f"q{d['question_id']}: key {d['stored']}, model {d['model']}" for d in disagreements]
    ) or None


def disputed_ids(disagreements: list[dict]) -> str | None:
    """The question ids to withhold this explanation for, comma-separated."""
    return ",".join(str(d["question_id"]) for d in disagreements) or None


def is_disputed(row: Explanation | None, question_id: int) -> bool:
    """Did the model contradict the answer key for *this* statement?

    If so the explanation may mislead about it specifically, even though it is sound
    about the rest of its cluster — so it is withheld here and served elsewhere.
    """
    if row is None or not row.disputed:
        return False
    return str(question_id) in row.disputed.split(",")


def is_fatal(exc: Exception) -> bool:
    """Will this fail identically for every other cluster?

    A rate limit or a dropped connection is worth retrying or skipping. A bad key, an
    exhausted quota or an unreachable model is not — and at runtime it means the
    feature is down rather than this one cluster being unlucky, which is a different
    thing to log.
    """
    if type(exc).__name__ in ("AuthenticationError", "PermissionDeniedError", "NotFoundError"):
        return True
    return any(
        marker in str(exc)
        for marker in (
            "invalid_api_key",
            "insufficient_quota",
            "model_not_found",
            "does not exist or you do not have access",
        )
    )


# --- the IO part -------------------------------------------------------------

async def cluster_members(session: AsyncSession, cluster_id: int) -> tuple[str, list[dict], str | None]:
    """(topic name, member rows, the cluster's figure)."""
    rows = (await session.execute(
        select(Question.id, Question.statement_it, Question.answer, Question.image_path, Topic.name)
        .join(Topic, Topic.id == Question.topic_id)
        .where(Question.cluster_id == cluster_id)
        .order_by(Question.id)
    )).all()
    members = [
        {"id": qid, "statement": statement, "answer": bool(answer)}
        for qid, statement, answer, _image, _topic in rows
    ]
    topic = rows[0][4] if rows else ""
    image = next((r[3] for r in rows if r[3]), None)
    return topic, members, image


async def existing(session: AsyncSession, cluster_id: int, lang: str) -> Explanation | None:
    return await session.scalar(
        select(Explanation).where(
            Explanation.cluster_id == cluster_id, Explanation.lang == lang
        )
    )


@dataclass(frozen=True)
class Outcome:
    """What one generation attempt did.

    The row alone is not enough for the batch caller, which has to report tokens and
    tell "the model declined" apart from "the call failed" — the first is expected and
    partly random, the second means stop.
    """

    outcome: str                        # stored | declined | error | no-article | no-members | no-key
    row: Explanation | None = None      # the Italian row, the canonical one
    tokens_in: int = 0
    tokens_out: int = 0
    fatal: bool = False
    detail: str = ""
    langs: tuple[str, ...] = ()


async def generate(
    session: AsyncSession, cluster_id: int, model: str | None = None
) -> Outcome:
    """Call the model for one cluster and store every language it returns.

    A stored row may be `flagged`, i.e. not servable — storing it is still right, so a
    reviewer can see what the model said. Never raises for an API problem: the caller's
    job is to serve or not serve, not to handle an OpenAI exception.
    """
    if not settings.openai_api_key:
        log.error("OPENAI_API_KEY is not set — explanations cannot be generated")
        return Outcome("no-key", fatal=True, detail="OPENAI_API_KEY is not set")

    corpus, index = corpus_and_index()
    topic, members, image = await cluster_members(session, cluster_id)
    if not members:
        return Outcome("no-members")

    judged = sample_statements(members)
    grounded = select_articles(topic, [m["statement"] for m in judged], corpus, index)
    if not grounded:
        log.warning("cluster %s has no article mapped for topic %r", cluster_id, topic)
        return Outcome("no-article", detail=topic)

    client = openai_client()
    kwargs = dict(
        model=model or settings.openai_model,
        messages=build_messages(topic, judged, grounded, image),
        response_format={"type": "json_object"},
    )
    try:
        try:
            response = await client.chat.completions.create(temperature=0, **kwargs)
        except Exception as exc:  # noqa: BLE001
            # Legal reasoning should not vary run to run, but not every model family
            # still accepts the parameter, so it is an attempt rather than a demand.
            if "temperature" not in str(exc):
                raise
            response = await client.chat.completions.create(**kwargs)
        parsed = json.loads(response.choices[0].message.content)
        usage = response.usage
        tokens = (usage.prompt_tokens, usage.completion_tokens) if usage else (0, 0)
    except Exception as exc:  # noqa: BLE001
        fatal = is_fatal(exc)
        log.error(
            "explanation generation failed for cluster %s: %s %s%s",
            cluster_id, type(exc).__name__, exc,
            " (fatal — the feature is down, not this cluster)" if fatal else "",
        )
        return Outcome("error", fatal=fatal, detail=f"{type(exc).__name__}: {exc}")

    texts = parsed_texts(parsed)
    if parsed.get("insufficiente") or LANG_IT not in texts:
        # Roughly a third of calls land here and it is partly run-to-run noise, so
        # nothing is written and the next request simply tries again. Italian missing
        # counts as a decline even if other languages came back: it is the canonical
        # text the gates and the reviewer both work from.
        log.info("model declined cluster %s: articles insufficient", cluster_id)
        return Outcome("declined", tokens_in=tokens[0], tokens_out=tokens[1])

    status, reasons, disagreements = check_gates(parsed, judged)
    flags = record_flags(reasons, disagreements)
    disputed = disputed_ids(disagreements)
    stored: dict[str, Explanation] = {}
    for code, text in texts.items():
        row = await existing(session, cluster_id, code)
        if row is None:
            row = Explanation(cluster_id=cluster_id, lang=code, text=text,
                              status=status, flags=flags, disputed=disputed)
            session.add(row)
        elif row.status == STATUS_APPROVED:
            # A human approved this wording. A regeneration must not quietly replace
            # it, in any language.
            continue
        else:
            row.text = text
            row.status = status
            row.flags = flags
            row.disputed = disputed
            row.reviewed_at = None
            row.reviewer = None
        stored[code] = row

    await session.commit()
    log.info("cluster %s -> %s in %s%s", cluster_id, status, ",".join(sorted(stored)),
             f" ({'; '.join(reasons)})" if reasons else "")
    return Outcome("stored", row=stored.get(LANG_IT), tokens_in=tokens[0],
                   tokens_out=tokens[1], detail="; ".join(reasons),
                   langs=tuple(sorted(stored)))


async def ensure(
    session: AsyncSession, cluster_id: int | None, lang: str = LANG_IT
) -> Explanation | None:
    """The explanation for this cluster in this language, generating if nobody has yet.

    Returns whatever is stored, servable or not — `servable()` is the separate decision,
    so a `flagged` row is not silently regenerated on every request in the hope of a
    better roll.
    """
    if cluster_id is None:
        return None

    row = await existing(session, cluster_id, lang)
    if row is not None:
        return row

    # Keyed on the cluster, not on (cluster, lang): one call produces every language, so
    # a Russian and an English reader arriving together should wait on the same call
    # rather than make two. Without it the loser of the race also overwrites the winner.
    lock = _locks.setdefault(cluster_id, asyncio.Lock())
    async with lock:
        row = await existing(session, cluster_id, lang)
        if row is not None:
            return row
        await generate(session, cluster_id)
        return await existing(session, cluster_id, lang)


async def warm(cluster_id: int | None, lang: str) -> None:
    """Produce the explanation before anyone asks for it, on its own session.

    Called as a background task when a question is *served*, so that by the time the
    user has read the statement and answered, the explanation is already cached and
    appears with the verdict instead of after a ten-second wait. Serving-time generation
    must never block the question itself — a wait before the statement even appears
    would cost more than any explanation is worth.

    Total spend does not change much by moving the trigger here: results are cached per
    cluster, so the ceiling is the 3382 clusters either way. What changes is how quickly
    that ceiling is approached, and callers gate on entitlement so a free user with no
    tasters left never triggers a call they could not be shown.

    Runs after the response has been sent, so it needs its own session — the request's
    is closed by then — and it must never raise into the event loop.
    """
    if cluster_id is None:
        return
    try:
        factory = async_session_factory()
        async with factory() as session:
            await ensure(session, cluster_id, lang)
    except Exception:  # noqa: BLE001
        # A failed warm is a slower explanation later, not a failed request now.
        log.warning("warming cluster %s (%s) failed", cluster_id, lang, exc_info=True)


def servable(row: Explanation | None) -> bool:
    """`draft` and `approved` reach users; `flagged` and `rejected` do not.

    The gates are the automatic quality bar now that no human sees a draft before its
    first reader does. Anything they distrusted reads as "not written yet" rather than
    as an answer — see STATUS.md §13.
    """
    return row is not None and row.status in SERVABLE_STATUSES


async def deliver(
    session: AsyncSession,
    question: Question,
    user,
    entitlement: Entitlement,
    *,
    generate_if_missing: bool = True,
) -> tuple[dict, Access]:
    """Hand over the explanation, or say why not. The only place a taster is spent.

    Both paths come through here so that entitlement, the taster and the conversion
    events have exactly one definition — two frontends and two endpoints must not hold
    three opinions about what "converted" means.

    `generate_if_missing` is the only difference between them. Answering passes False:
    the explanation should already be warmed, and paying for a call at that moment would
    charge for the majority who answer and move on. The explicit "why" request passes
    True, because the user is standing there having asked.
    """
    if question.cluster_id is None:
        return {"explanation_state": Access.UNAVAILABLE.value, "explanation": None}, \
            Access.UNAVAILABLE

    # Checked before any API call, which is the whole point of checking here: never pay
    # to produce something this user cannot be shown.
    if not entitlement.can_explain:
        await events.record(
            session,
            EV_PAYWALL_HIT,
            chat_id=user.chat_id,
            question_id=question.id,
            topic_id=question.topic_id,
        )
        await session.commit()
        return {"explanation_state": Access.LOCKED.value, "explanation": None}, Access.LOCKED

    # A UI language we do not write explanations in reads one in another language rather
    # than being told nothing exists. Uzbek falls back to Russian: it is the language this
    # market already shares, and "unavailable" would be a worse answer than "available, in
    # Russian". Without this, choosing Uzbek would silently switch explanations off.
    lang = EXPLANATION_FALLBACK.get(user.lang, user.lang)

    if generate_if_missing:
        row = await ensure(session, question.cluster_id, lang)
    else:
        row = await existing(session, question.cluster_id, lang)

    if not servable(row) or is_disputed(row, question.id):
        # The model declined, the call failed, a gate withheld the cluster, this
        # particular statement is one the model contradicted, or warming has not
        # finished. All read the same to a user, and none of them costs a taster — but
        # only the last is worth offering a button for.
        access = (
            Access.AVAILABLE
            if row is None and not generate_if_missing
            else Access.UNAVAILABLE
        )
        return {"explanation_state": access.value, "explanation": None}, access

    if entitlement.spends_free_explanation:
        user.free_explanations_used += 1
    await events.record(
        session,
        EV_EXPLANATION_VIEWED,
        chat_id=user.chat_id,
        question_id=question.id,
        topic_id=question.topic_id,
        free_taster=entitlement.spends_free_explanation,
        # Whether the first reader of this text was a user rather than a reviewer.
        # The share of served explanations that are unreviewed drafts is the quality
        # risk STATUS.md §13 accepts, and it is only measurable if it is logged.
        status=row.status,
    )
    await session.commit()
    return {"explanation_state": Access.SHOWN.value, "explanation": row.text}, Access.SHOWN
