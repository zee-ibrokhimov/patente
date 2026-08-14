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

ALL FOUR LANGUAGES COME BACK IN ONE CALL
----------------------------------------
Plan §3.3 had a separate pass translating the *approved* Italian into RU and EN, so that
legal substance was reviewed once and translation fidelity separately. That pass is not
being written (STATUS.md §13): the same request returns Italian, Russian, English and
Uzbek, and four rows are stored.

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
import math
import mimetypes
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
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
    EV_MODEL_CALL,
    DEFAULT_LANG,
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

# How much of the budget is reserved for the topic's OWN articles before sign-name matches
# get any. Without a reservation the floor is not a floor: one long matched article can
# consume everything, and cluster 638 shipped a flatly wrong explanation because it did —
# see select_articles.
#
# Half rather than all, because for a sign cluster the article naming that sign genuinely is
# the most relevant thing and should still lead.
FLOOR_SHARE = 0.5

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

4. Fornisci la STESSA spiegazione anche in russo, in inglese e in uzbeko. Non è una
   traduzione letterale da rivedere a parte: è la stessa regola espressa nelle quattro
   lingue. I riferimenti agli articoli restano nella forma italiana ("art. 148 C.d.S.")
   in tutte le lingue, perché è così che il candidato li troverà nel codice.

5. L'uzbeco ("uz") va scritto in uzbeko moderno in alfabeto LATINO, quello ufficiale in
   Uzbekistan: "yo'l belgisi", non "йўл белгиси" e non testo russo. Non lasciare parole
   russe non tradotte e non traslitterare il russo in caratteri latini. Se non sai
   scrivere la spiegazione in uzbeko, ometti la chiave "uz": una lingua mancante è
   accettabile, una lingua sbagliata no.

Se gli articoli forniti non bastano a decidere, imposta "insufficiente": true e lascia
"spiegazione" vuota. NON inventare e NON tirare a indovinare: una spiegazione assente è
accettabile, una spiegazione sbagliata non lo è.

Rispondi SOLO con un oggetto JSON di questa forma esatta:

{"insufficiente": false,
 "spiegazione": {"it": "...", "ru": "...", "en": "...", "uz": "..."},
 "articolo_citato": "art. 148 C.d.S.",
 "segnale_riconosciuto": "DARE PRECEDENZA",
 "verdetti": [{"n": 1, "risposta": "VERO", "certezza": "alta"}]}

"risposta" è "VERO" o "FALSO". "certezza" è "alta", "media" o "bassa".
"segnale_riconosciuto" è il nome del segnale nell'immagine, o "" se non c'è immagine.
Includi un verdetto per ogni affermazione, con "n" pari al suo numero.
"""

# The last resort, for questions the Codice does not answer.
#
# Part of the ministerial syllabus is simply not statute. "Cover the wounds of a casualty
# with fractures", "put an unconscious casualty in the recovery position", "failing to obey
# the rules for merging results in..." — these are first aid, physiology and driving
# practice. Art. 189 says stop and assist; it does not say how to treat a fracture. Handed
# only the Codice, the model declines, and it is RIGHT to: the answer is not in what it was
# given. Retrieval cannot fix that, because the text does not exist to retrieve.
#
# So the third attempt changes the question rather than the search: explain from the
# standard driving-theory syllabus, and say plainly that no article settles it. The one
# thing forbidden is the failure mode this whole module is built around — inventing a
# citation. A wrong article number is worse than no article number, because it is checkable
# and a learner who checks it loses trust in everything else on the screen.
SYLLABUS_PROMPT = SYSTEM_PROMPT.replace(
    """Se gli articoli forniti non bastano a decidere, imposta "insufficiente": true e lascia
"spiegazione" vuota. NON inventare e NON tirare a indovinare: una spiegazione assente è
accettabile, una spiegazione sbagliata non lo è.""",
    """Gli articoli forniti NON bastano a decidere: è già stato verificato. Questa domanda
appartiene al programma d'esame ma non è disciplinata dal Codice della Strada — per esempio
il primo soccorso, la fisiologia della guida, la meccanica del veicolo o le conseguenze
pratiche di un comportamento.

Spiega quindi la regola secondo il PROGRAMMA D'ESAME ufficiale per la patente B e la
pratica di guida corrente, non secondo gli articoli.

NON citare alcun articolo e lascia "articolo_citato" vuoto. Inventare un numero di articolo
è l'errore più grave possibile: è verificabile, e un candidato che lo verifica perde
fiducia in tutto il resto. Se davvero non conosci la risposta corretta, imposta
"insufficiente": true.""",
)

# One lock per cluster. Ten users answering the same question at once would otherwise
# be ten identical paid calls, and the loser of the race would overwrite the winner.
_locks: dict[int, asyncio.Lock] = {}

# Clusters a language was asked for and did not come back with, and when.
#
# `ensure` is keyed on the REQUESTED language, which is what lets a new language backfill
# itself: a cluster cached in it/ru/en before Uzbek existed misses on uz and regenerates.
# The other half of that is this. Without it, a cluster the model will not write Uzbek for
# regenerates on EVERY request from every Uzbek reader — a paid call each time, forever,
# for a row that never appears. Translations hit exactly this and were fixed the same way;
# adding Uzbek to explanations opens the identical hole here.
#
# In-process and deliberately not persisted: a restart is a fine moment to try again, and
# the alternative is a schema for remembering failures.
_missing: dict[tuple[int, str], float] = {}
RETRY_AFTER = 3600.0


def _recently_failed(cluster_id: int, lang: str, now: float | None = None) -> bool:
    when = _missing.get((cluster_id, lang))
    if when is None:
        return False
    now = now if now is not None else time.monotonic()
    if now - when > RETRY_AFTER:
        _missing.pop((cluster_id, lang), None)
        return False
    return True

# The corpus is ~1.5M characters of JSON and never changes at runtime.
_corpus: dict | None = None
_index: dict | None = None


# How long a model call may hold a database connection.
#
# The SDK's default is 600 SECONDS, and every generation path holds the request's session
# across the call — `ensure` passes it straight into `generate`. SQLAlchemy's async pool
# is five connections plus ten overflow, so fifteen requests waiting on a slow OpenAI
# exhaust it and the API stops answering anything at all: profile, stats, the exam timer.
# A third-party being slow would have become a total outage of a product that is mostly
# not about that third party.
#
# Was 45s, against measured cold times of ~5s. Raised to 90s on 2026-08-09 when explanations
# moved to gpt-5-mini at full reasoning: measured 23.7s average with one cluster at 42.8s,
# because it emits ~5000 reasoning tokens the learner never sees. At 45s that cluster was
# one bad run from timing out.
#
# Affordable now for the reason the model changed at all: explanations are PREPARED five
# questions ahead, on their own session, while a start screen is showing. Nobody is watching
# the clock, and the pool exhaustion this bound exists to prevent needs a request holding a
# connection — which a background warm does not.
#
# It is still a bound. Without one a hung call holds a connection until the process dies.
OPENAI_TIMEOUT = 90.0
OPENAI_RETRIES = 1


def openai_client():
    """The client, behind a function so a test can replace it.

    Imported lazily so that importing this module — which the API does at startup —
    does not depend on the SDK being installed, and so the substitution point is a
    module attribute rather than an import inside a function body.
    """
    from openai import AsyncOpenAI

    return AsyncOpenAI(
        api_key=settings.openai_api_key,
        timeout=OPENAI_TIMEOUT,
        max_retries=OPENAI_RETRIES,
    )


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


WORD_RE = re.compile(r"[a-zà-ú]{4,}")

# How hard to penalise a long article, as BM25's `b`. SWEPT, not chosen by taste, against
# the two clusters whose failure prompted this — measured rank of the article that actually
# answers each:
#
#     b      reg.139 vs cds.41 (dashed line)     cds.57 (macchine agricole)
#     0.00   FAILS — 139 second                  rank 2
#     0.15   139 first, 41 second                rank 3
#     0.25   139 first, 41 third                 rank 5     <- chosen
#     0.50   139 second, 41 sixth                rank 30
#     0.75   139 second, 41 fourteenth           rank 44
#
# 0 leaves the length bias unchecked and a 9,944-character article about traffic lights wins
# a road-markings cluster. Past ~0.35 it over-corrects the other way, and 200-character
# stubs — "Dispositivo retrovisore delle macchine agricole" — beat the governing article.
# 0.25 clears both with margin. Two clusters is a thin basis; widen the sample before
# trusting a smaller change to this number.
LENGTH_B = 0.25

# Words that appear in nearly every article of the Codice and so identify nothing. Kept
# deliberately short: the IDF weighting below already suppresses common terms, and a long
# hand-written list is a way to accidentally discard the word that mattered.
STOPWORDS = frozenset("""
alla alle allo agli anche articolo caso casi come consentito dalla dalle dallo degli deve
devono essere fine fini deve nonche oppure ovvero parte parti possono presente quale quali
quando quanto quello questa queste questi questo sono sull sulla sulle sullo tale tali
tutti tutte essere avere altri altro altre nella nelle nello negli
""".split())


def rank_for(references, statements: list[str], corpus: dict) -> list[tuple[str, str]]:
    """Order a topic's articles by how much they look like THIS cluster.

    A topic's articles arrive in the order somebody mapped them, which is a property of the
    topic and identical for all of its clusters. With 17 of 25 topics larger than the prompt
    budget, that meant a fixed prefix of the statute answered every question in the topic —
    and a cluster whose rule lived further down was shown law that could not settle it.

    Scored by rare-word overlap rather than raw overlap. Weighting every shared word equally
    ranks by article LENGTH, because a long article shares more of everything: "veicolo" and
    "strada" appear in most of the Codice and separate nothing, while "inversione" or
    "cunetta" appear in one or two articles and are close to an identification. The inverse
    document frequency here is computed over the topic's own articles, which is the set the
    ranking has to discriminate between.

    Deliberately lexical. A vector store would rank better and would be a new dependency, a
    new failure mode and an index to keep in step with the corpus, for a corpus of a few
    hundred articles that changes when the ministry reissues it. Stable, offline, and
    explainable is worth more here than the last few points of recall.

    Ties keep the hand-mapped order, so the previous behaviour is what happens when nothing
    in the statements discriminates at all.
    """
    candidates = [(source, number) for source, number in references
                  if corpus[source].get(number) is not None]
    if len(candidates) < 2:
        return list(references)

    texts = {ref: corpus[ref[0]][ref[1]]["text"].lower() for ref in candidates}
    rubrics = {ref: (corpus[ref[0]][ref[1]].get("rubric") or "").lower() for ref in candidates}

    terms = {w for w in WORD_RE.findall(" ".join(statements).lower()) if w not in STOPWORDS}
    if not terms:
        return list(references)

    total = len(candidates)
    # Document frequencies once, not once per (article, term). The inner loop used to
    # rescan every candidate for every term of every article — fine for a topic's dozen
    # articles, quadratic and unusable over the whole corpus, which is what the
    # widened search below needs.
    holders = {term: sum(1 for ref in candidates if term in texts[ref]) for term in terms}
    weights = {term: math.log(total / n)
               for term, n in holders.items() if n and n != total}

    # Length normalisation, as BM25 does it. Presence-scoring still favours long articles,
    # because a longer text simply contains more distinct words: for the dashed-line cluster,
    # `cds.41 "Segnali luminosi"` scored top on 9,944 characters of incidental overlap and
    # ate 9,944 of a 12,000-character reservation, pushing out `reg.139 "Strisce di
    # separazione dei sensi di marcia"` — which ranked SECOND and needed only 3,500. Getting
    # the order right is not enough when first place can spend the whole budget.
    average = sum(len(texts[ref]) for ref in candidates) / total

    scores: dict[tuple[str, str], float] = {}
    for ref in candidates:
        body, rubric = texts[ref], rubrics[ref]
        score = 0.0
        for term, weight in weights.items():
            if term in body:
                score += weight
            # The rubric is the article's own title — "Strisce di separazione dei sensi di
            # marcia". A hit there is worth far more than a hit in the body, where the word
            # may appear once in a subordinate clause.
            if term in rubric:
                score += weight * 3
        scores[ref] = score / (1 - LENGTH_B + LENGTH_B * len(texts[ref]) / average)

    order = {ref: i for i, ref in enumerate(references)}
    return sorted(candidates, key=lambda ref: (-scores[ref], order[ref]))


def select_articles(
    topic_name: str,
    statements: list[str],
    corpus: dict,
    index: dict,
    budget: int = CONTEXT_CHARS,
) -> list[dict]:
    """The articles to put in front of the model, most specific first.

    Sign-name matches come first because they name the article defining the sign in
    question; the topic's hand-mapped articles are the FLOOR, so no cluster is ever sent
    with no statute at all. Matches are used only for ordering — a mention is not an
    identification, and being wrong there should cost some irrelevant text rather than a
    wrong answer.

    THE FLOOR HAS TO BE RESERVED, OR IT IS NOT A FLOOR

    It was not. Sign matches went first and the budget was filled in order, so a long
    matched article could consume everything and leave nothing of the topic's own.

    Cluster 638 is what that looks like in production. It is about a dashed white line;
    `signs_in` matched art. 135 "Segnali utili per la guida", which is 16,208 characters —
    two thirds of the whole budget. With arts. 122 and 148 behind it, every article the
    topic actually maps to was skipped, including art. 139 "Strisce di separazione dei
    sensi di marcia". The model was asked to explain a road MARKING while being shown
    articles about road SIGNS, and the statute that says a dashed line separates the
    directions of travel was not in the prompt at all.

    It then wrote "Non è destinata a separare i sensi di marcia" — the exact opposite of
    the ministerial answer to q20711 in its own cluster. That is not a hallucination; it is
    a correct reading of the wrong law.

    So the topic's articles now get a reserved share of the budget, filled FIRST, and sign
    matches compete for the rest. Sign clusters are unaffected in practice: the article
    defining a sign is almost always inside its own topic's mapping, so it is taken by the
    reservation rather than despite it.
    """
    matched = signs_in(statements, index)
    mapped = articles_for(topic_name)

    def take(references, allowance, chosen, seen, used):
        for source, number in references:
            if (source, number) in seen:
                continue
            article = corpus[source].get(number)
            if article is None:              # repealed, or never fetched
                continue
            size = len(article["text"])
            # `used and ...` keeps the original behaviour that one oversized article is
            # better than none: a cluster with a single 30k-character governing article
            # still gets it rather than being sent bare.
            if used and used + size > allowance:
                continue
            seen.add((source, number))
            chosen.append({"source": source, "number": number, **article})
            used += size
        return used

    chosen: list[dict] = []
    seen: set[tuple[str, str]] = set()

    # The floor first, inside its reservation — RANKED, not in mapping order.
    #
    # Order was the whole problem. `articles_for` returns a topic's articles in the order
    # they were hand-mapped, and 17 of the 25 topics hold more statute than the budget:
    # "Segnali di indicazione" is 100,844 characters against 24,000. So the floor was always
    # filled by whichever articles were listed first, and EVERY cluster in a large topic got
    # the same opening slice regardless of what it was about.
    #
    # When the article carrying the rule was not in that slice the model refused — correctly:
    # it is told to explain from the statute it is given, not from memory. Clusters 2823 and
    # 233 both declined twice with "articles insufficient", and only 43 of 3,382 clusters had
    # an explanation at all. Ranking by what the cluster actually says is the fix; the budget
    # is deliberately unchanged so the improvement is attributable to this and not to volume,
    # and because a bigger prompt runs into the account's 30,000 TPM ceiling.
    ranked = rank_for(mapped, statements, corpus)
    used = take(ranked, budget * FLOOR_SHARE, chosen, seen, 0)
    # Then the sign matches, against the full budget.
    used = take(matched, budget, chosen, seen, used)

    # Then the best of the WHOLE CORPUS — before the topic's leftovers, not after.
    #
    # The topic mappings are hand-written and necessarily incomplete. Cluster 233 asks
    # whether macchine agricole may drive on the road; `cds.57 "Macchine agricole"`,
    # cds.104 and cds.114 all sit in the corpus and NONE of them is mapped to "Definizioni
    # stradali e di traffico". The model was asked about agricultural machinery while being
    # shown the definitions article, and declined — correctly.
    #
    # Ordering is the entire point of this being here rather than last. Ranked corpus-wide,
    # cds.57 comes SECOND for that cluster and the top ten are all machinery-registration
    # law. Run after the topic's leftovers it still lost, because eight mapped articles had
    # already spent the budget and only 295- and 2302-character scraps still fitted. The
    # floor keeps its reservation, so a topic never loses its own statute; what changes is
    # that the remainder goes to the best-matching law rather than to whatever the topic
    # listed next.
    everywhere = [(source, number) for source in corpus for number in corpus[source]]
    used = take(rank_for(everywhere, statements, corpus), budget, chosen, seen, used)

    # Anything of the floor still unplaced, last, as the guarantee that a topic is never
    # sent bare.
    take(ranked, budget, chosen, seen, used)
    return chosen


def build_text_prompt(topic: str, judged: list[dict], grounded: list[dict],
                      with_key: bool = False) -> str:
    """The prompt. `with_key` hands the model the OFFICIAL ANSWERS.

    Off by default, and that default is what makes the answer-key gate work at all: the
    model judges each statement independently, and a disagreement is evidence worth acting
    on — it is how a wrong explanation is caught before a learner reads it.

    On only for a REWRITE of a cluster the gate already withheld for disagreeing. At that
    point the independent judgement has been made and recorded; asking again without the key
    would produce the same disagreement and the same withheld text, and the learner would go
    on seeing nothing. The exam is graded against the ministry's answer, so an explanation
    that teaches why the official answer is the official answer is the useful thing to write.

    The disagreement stays on the row either way. This changes what the learner is shown,
    not what the reviewer is told.
    """
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
    if with_key:
        parts += [
            "",
            "RISPOSTE UFFICIALI DEL MINISTERO (vincolanti):",
        ]
        parts += [f"{i}. {'VERO' if m['answer'] else 'FALSO'}"
                  for i, m in enumerate(judged, 1)]
        parts += [
            "",
            "Queste risposte sono quelle su cui il candidato viene esaminato. Spiega "
            "PERCHÉ ciascuna risposta ufficiale è quella indicata, citando l'articolo. "
            "Non contestarle. Se un'affermazione ti sembra discutibile, spiega la "
            "distinzione che la rende vera o falsa secondo la norma. "
            "I `verdetti` devono coincidere con le risposte ufficiali.",
        ]
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
    topic: str,
    judged: list[dict],
    grounded: list[dict],
    image_path: str | None,
    syllabus: bool = False,
    with_key: bool = False,
) -> list[dict]:
    content: list[dict] = [
        {"type": "text", "text": build_text_prompt(topic, judged, grounded, with_key)}
    ]
    figure = figure_part(image_path)
    if figure:
        content.append(figure)
    return [
        {"role": "system", "content": SYLLABUS_PROMPT if syllabus else SYSTEM_PROMPT},
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


def ungrounded_numbers(text: str, articles: list[dict]) -> list[str]:
    """Numbers in the explanation that do not appear in the statute it was given.

    THE GATE THIS REPLACES BANNED ALL DIGITS, and it was the single biggest reason a
    learner was told "not available yet". Measured on live data: 9 of the 10 withheld rows
    were withheld by it, including explanations that were quoting the article correctly.

    The fear behind it is right — a wrong speed limit is the worst thing this product can
    say, and the reviewer had learned to ignore a gate that fired on every draft. But
    "contains a digit" is not the same property as "invented a figure". An explanation that
    says 50 km/h because the article in front of it says 50 km/h is exactly what the
    feature is for; one that says 70 when the statute says 50 is the actual danger, and the
    old gate could not tell them apart, so it withheld both.

    This checks grounding instead: every number in the text must appear in the article text
    the model was shown. That passes quoted figures and catches invented ones, which is the
    distinction that matters.

    Deliberately strict about what counts as grounded — the number must appear literally.
    A figure that is right but rephrased ("cinquanta") will still flag, and that is the
    correct direction to fail in.
    """
    stripped = CITATION_RE.sub("", text)
    # Bare digits only. Unit words on their own ("metri", "km/h") say nothing without a
    # figure attached, and gating on them is what made the old rule fire on almost
    # everything.
    numbers = re.findall(r"\d+(?:[.,]\d+)?", stripped)
    if not numbers:
        return []
    corpus = " ".join(a.get("text", "") for a in articles)
    if not corpus:
        # No statute to check against — fall back to the old, blunt behaviour rather than
        # passing everything, because an unchecked number is the thing being guarded.
        return sorted(set(numbers))
    # Normalise the decimal separator: the corpus writes 3,5 and a model may write 3.5.
    haystack = corpus.replace(",", ".")
    return sorted({n for n in numbers if n.replace(",", ".") not in haystack})


def check_gates(
    parsed: dict, judged: list[dict], articles: list[dict] | None = None
) -> tuple[str, list[str], list[dict]]:
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
    invented = ungrounded_numbers(italian, articles or [])
    if invented:
        reasons.append("contains a number that is not in the cited article: "
                       + ", ".join(invented))
    if any(
        str(v.get("certezza", "")).lower() == "bassa"
        for v in parsed.get("verdetti", [])
        if isinstance(v, dict)
    ):
        reasons.append("model reports low confidence")

    return (STATUS_FLAGGED if reasons else STATUS_DRAFT), reasons, disagreements


# Notes about WHERE a row came from, as opposed to what is wrong with it. Appended to the
# reasons so they land in `flags` and stay visible for ever, but deliberately AFTER the
# status has been decided — neither of them withholds anything.
SYLLABUS_NOTE = "explained from the exam syllabus — no article covers it"
SUPPLIED_KEY_NOTE = ("rewritten with the official answers supplied — the verdicts are not "
                     "independent")


def provenance(reasons: list[str], attempt: int = 0, with_key: bool = False) -> list[str]:
    """Reasons, plus how this row was produced.

    Both notes exist because a row read six months later says nothing about the conditions
    it was written under, and both change how it should be read:

      · the syllabus note: this text cites no article BY DESIGN, because no article covers
        the question. A reviewer comparing it against the Codice would otherwise conclude
        the citation had gone missing.
      · the supplied-key note: the model was handed the ministry's answers, so its verdicts
        agreeing with them is not independent corroboration and must never be read as such.
        The original disagreement is still on the row this replaced, which is where a
        reviewer looks to decide whether the model or the answer key was wrong.

    A separate function because the status is decided by `check_gates` BEFORE this runs, and
    a note appended to `reasons` that accidentally flagged a row would withhold every
    syllabus-written explanation in the bank.
    """
    out = list(reasons)
    if attempt >= 2:
        out.append(SYLLABUS_NOTE)
    if with_key:
        out.append(SUPPLIED_KEY_NOTE)
    return out


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
    session: AsyncSession, cluster_id: int, model: str | None = None, attempt: int = 0,
    with_key: bool = False,
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
        messages=build_messages(topic, judged, grounded, image,
                                syllabus=attempt >= 2, with_key=with_key),
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
        # Roughly a third of calls land here and it is PARTLY RUN-TO-RUN NOISE — which is
        # the whole argument for asking twice. The learner is standing there having tapped
        # "Why?", and "not available yet" for a cluster that would have answered on a
        # second roll is the most annoying way to fail: nothing is wrong, it just did not
        # try. One retry, not a loop, because a cluster whose articles genuinely do not
        # cover the statements will decline every time and paying repeatedly to hear that
        # is how a per-request cost becomes unbounded.
        #
        # Italian missing counts as a decline even if other languages came back: it is the
        # canonical text the gates and the reviewer both work from.
        # Attempt 0 -> ask again (noise). Attempt 1 -> ask WITHOUT the statute requirement,
        # because two refusals on the same articles is not noise, it is the articles.
        #
        # Part of the ministerial syllabus is not law: first aid, physiology, vehicle
        # mechanics, the practical consequences of a behaviour. Clusters 2823 and 2814 ask
        # how to treat a casualty; 1487 asks what failing to obey the merging rules leads
        # to. No retrieval reaches an answer that was never written into the Codice, so the
        # third attempt changes the question instead of the search — see SYLLABUS_PROMPT.
        if attempt < 2:
            log.info("cluster %s declined, asking again (attempt %s)", cluster_id, attempt + 1)
            again = await generate(session, cluster_id, model, attempt=attempt + 1)
            # Carry this attempt's tokens so the caller's accounting is honest.
            return Outcome(again.outcome, row=again.row,
                           tokens_in=again.tokens_in + tokens[0],
                           tokens_out=again.tokens_out + tokens[1],
                           fatal=again.fatal, detail=again.detail, langs=again.langs)
        log.info("cluster %s declined even without the statute requirement", cluster_id)
        return Outcome("declined", tokens_in=tokens[0], tokens_out=tokens[1])

    # In syllabus mode the articles are NOT what the answer rests on, so they cannot ground
    # its numbers. Passing an empty list makes every number in the text ungrounded and
    # therefore withholds the cluster — deliberately strict. A first-aid explanation
    # normally carries no figures at all, but "keep 3 metres back" or "under 50 km/h" from
    # a model working without a source is exactly the sentence this product must never
    # print, and there is nothing here to check it against.
    status, reasons, disagreements = check_gates(
        parsed, judged, [] if attempt >= 2 else grounded)
    reasons = provenance(reasons, attempt=attempt, with_key=with_key)
    flags = record_flags(reasons, disagreements)
    disputed = disputed_ids(disagreements)
    stored: dict[str, Explanation] = {}
    for code, text in texts.items():
        row = await existing(session, cluster_id, code)
        if row is None:
            row = Explanation(cluster_id=cluster_id, lang=code, text=text,
                              status=status, flags=flags, disputed=disputed,
                              generated_at=datetime.now(timezone.utc))
            session.add(row)
        elif row.status == STATUS_APPROVED:
            # A human approved this wording. A regeneration must not quietly replace
            # it, in any language.
            continue
        elif servable(row) and status not in SERVABLE_STATUSES:
            # A REGENERATION MAY IMPROVE A ROW, NEVER DEMOTE ONE.
            #
            # Found by running the Uzbek backfill against production. Most regenerations
            # now exist to fill in a MISSING language, and they re-roll the whole cluster:
            # new text, new gates, new status for every language. The gates are partly
            # luck — the numeric one fires on any digit in the fresh Italian — so a cluster
            # that was `draft` can come back `flagged`.
            #
            # Cluster 306 did exactly that. It was servable in it/ru/en, an Uzbek row was
            # requested, the new Italian said "M1" where the old one had not, and all four
            # languages became `flagged`. An Uzbek reader asking one question had silently
            # revoked a working explanation for every Russian and English reader of that
            # cluster, and nothing anywhere would have reported it.
            #
            # The old text passed the gates when it was written, so keeping it is sound.
            # The NEW language is still judged on its own merits below — if this roll is
            # bad, Uzbek is withheld and falls back, which is the correct outcome for the
            # reader who triggered it and costs nobody else anything.
            log.info("cluster %s: keeping the servable %s row rather than replacing it "
                     "with a %s one (%s)", cluster_id, code, status, flags)
            continue
        else:
            row.text = text
            row.status = status
            row.flags = flags
            row.disputed = disputed
            row.generated_at = datetime.now(timezone.utc)
            row.reviewed_at = None
            row.reviewer = None
        stored[code] = row

    await session.commit()
    log.info("cluster %s -> %s in %s%s", cluster_id, status, ",".join(sorted(stored)),
             f" ({'; '.join(reasons)})" if reasons else "")
    # What that cost. See EV_MODEL_CALL — the numbers were computed on every call and
    # discarded by every caller, so nothing in the product could say what it spends.
    await events.record(session, EV_MODEL_CALL, kind="explanation",
                        cluster_id=cluster_id, model=model or settings.openai_model,
                        tokens_in=tokens[0], tokens_out=tokens[1])
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
    if _recently_failed(cluster_id, lang):
        return None

    # The LOCK is keyed on the cluster, not on (cluster, lang): one call produces every
    # language, so a Russian and an English reader arriving together should wait on the
    # same call rather than make two. Without it the loser of the race also overwrites
    # the winner. The MISS is keyed on both, because "this cluster has no Uzbek" must not
    # stop a Russian reader who has a perfectly good row.
    lock = _locks.setdefault(cluster_id, asyncio.Lock())
    async with lock:
        row = await existing(session, cluster_id, lang)
        if row is not None:
            return row
        await generate(session, cluster_id)
        row = await existing(session, cluster_id, lang)

    if row is None:
        _missing[(cluster_id, lang)] = time.monotonic()
        log.warning("cluster %s produced no %s explanation — not retrying for an hour",
                    cluster_id, lang)
    return row


async def withheld_clusters(session: AsyncSession, limit: int = 50) -> list[int]:
    """Clusters whose only explanation is one the gates refused to serve.

    Biggest first, because a withheld cluster is a hole exactly as wide as the number of
    questions in it — the same reason `content/generate` writes the big ones first.
    """
    servable_ids = select(Explanation.cluster_id).where(
        Explanation.status.in_(SERVABLE_STATUSES))
    flagged_ids = select(Explanation.cluster_id).where(
        Explanation.status == STATUS_FLAGGED)
    rows = await session.execute(
        select(Question.cluster_id, func.count(Question.id).label("n"))
        .where(Question.cluster_id.in_(flagged_ids),
               Question.cluster_id.not_in(servable_ids))
        .group_by(Question.cluster_id)
        .order_by(func.count(Question.id).desc())
        .limit(limit)
    )
    return [cid for cid, _n in rows]


async def rewrite_withheld(session: AsyncSession, limit: int = 50) -> dict:
    """Write a fresh explanation for every cluster the gates withheld, with the key supplied.

    WHY THIS IS NOT SIMPLY "TRY AGAIN". A cluster is withheld for one of four measured
    reasons, and only one of them is fixed by asking again:

      · it argued against the ministry's answer — asking again reproduces the argument,
        because the model has not changed its mind. Handing it the official answers turns
        the task from "judge this" into "explain why the examiner's answer is what it is",
        which is the thing a learner actually needs;
      · it cited a number the article does not contain — a REAL defect, and the worst thing
        this product can say. Regenerating may or may not fix it and the gate will catch it
        again either way. Nothing here bypasses that check;
      · low confidence, or no article covers it — the second attempt falls back to the exam
        syllabus, which is the existing `attempt >= 2` path.

    So this is not a bypass. It changes the QUESTION the model is asked; every gate still
    runs on the answer, and a rewrite that invents a number is withheld exactly as before.
    """
    targets = await withheld_clusters(session, limit)
    done = {"clusters": len(targets), "served": 0, "still_withheld": 0, "failed": 0}
    for cluster_id in targets:
        outcome = await generate(session, cluster_id, with_key=True)
        if outcome.outcome != "stored":
            done["failed"] += 1
            log.info("rewrite of cluster %s did not store: %s", cluster_id, outcome.outcome)
            await session.rollback()
            continue
        row = await existing(session, cluster_id, LANG_IT)
        if servable(row):
            done["served"] += 1
        else:
            done["still_withheld"] += 1
        # COMMITTED PER CLUSTER, and this is not tidiness.
        #
        # The first version committed once at the end, so one transaction stayed open across
        # sixteen model calls — half a minute of network with the session dirty. Every SELECT
        # in the loop then had to upgrade that transaction to a write, which SQLite refuses
        # outright when anything else holds the lock, and the whole run died with "database
        # is locked" after 36 seconds. Found by running it against production, not by a test.
        #
        # Per-cluster commits also make the work durable: a failure on cluster nine keeps
        # the eight already rewritten, instead of throwing the money away.
        await session.commit()
    log.info("rewrote %s withheld clusters: %s", len(targets), done)
    return done


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

    # The reader's OWN language first, and another only if this particular cluster has
    # nothing servable in it.
    #
    # This used to redirect before looking: `EXPLANATION_FALLBACK.get(user.lang)` sent every
    # Uzbek reader to Russian unconditionally, because Uzbek explanations were not written
    # at all. They are now, so the fallback is what it should always have been — a per-
    # cluster safety net for the cases where the model declined that one language, rather
    # than a standing decision about a whole audience.
    # The READING language, the same one the question is translated into. A learner who has
    # set questions to Russian while the app stays in Uzbek is telling us which language they
    # study in; serving the question in Russian and the explanation under it in Uzbek would
    # split one act of reading across two languages.
    from api.services.translations import reading_language
    reads = reading_language(user)
    wanted = reads if reads in EXPLANATION_LANGUAGES else DEFAULT_LANG
    order = [wanted]
    fallback = EXPLANATION_FALLBACK.get(reads)
    if fallback and fallback not in order:
        order.append(fallback)

    row: Explanation | None = None
    lang = wanted
    cached_anything = False
    for candidate in order:
        found = (
            await ensure(session, question.cluster_id, candidate)
            if generate_if_missing
            else await existing(session, question.cluster_id, candidate)
        )
        cached_anything = cached_anything or found is not None
        if servable(found) and not is_disputed(found, question.id):
            row, lang = found, candidate
            break

    # Costs at most one model call, not one per language: the first `ensure` generates
    # every language at once, so the second candidate is already a cache hit.

    if row is None:
        # The model declined, the call failed, a gate withheld the cluster, this
        # particular statement is one the model contradicted, or warming has not
        # finished. All read the same to a user, and none of them costs a taster — but
        # only the last is worth offering a button for.
        access = (
            Access.AVAILABLE
            if not cached_anything and not generate_if_missing
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
    return (
        {"explanation_state": Access.SHOWN.value, "explanation": row.text,
         "explanation_lang": lang},
        Access.SHOWN,
    )
