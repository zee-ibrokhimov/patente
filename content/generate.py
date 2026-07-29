"""
generate.py — write the canonical Italian explanation for each rule cluster.

Build step 6, plan §3.3 steps 4-5. This is milestone M2: the run that finally
measures the correction rate the whole schedule is guessed from.

Generation is one-time and offline. Nothing here ever runs while a user is waiting,
which is why accuracy is worth paying for and latency is worth nothing.

THE ANSWER KEY IS A TEST, NOT AN INPUT
--------------------------------------
The obvious prompt hands the model the statement, the correct answer and the
article, and asks it to justify the answer. That prompt cannot fail. Whatever the
key says, the model will produce fluent Italian agreeing with it — including when
the key is wrong because `extract.py` slipped a row, and including when the model
has no idea.

So the key is withheld. The model is given the statements and the article text and
asked to decide VERO or FALSO itself, and only then to explain the rule. The stored
answer is compared to its verdicts afterwards. Any disagreement flags the cluster,
which is plan §3.3's second quality gate implemented as a real test:

  · the model reasoned badly about the law, or
  · the statement is not governed by the article this topic was mapped to, or
  · the ministerial answer in our database is wrong — a parsing error that
    survived extraction, and the one class of defect that poisons everything
    downstream.

All three need a human. Which of the three it is, only a human can say.

NAMING THE SIGN: TRIED, MEASURED, REVERTED
------------------------------------------
Most sign statements say only "il segnale raffigurato", and the model cannot see the
figure — so telling it which sign is depicted looked like an obvious win, and on one
cluster it was (verdict agreement 11/12 -> 12/12). It is not available, because the
sign cannot be identified from the statements:

    cluster 623 is DARE PRECEDENZA, but one of its statements mentions the
    PREAVVISO DI DARE PRECEDENZA that precedes it;
    cluster 624 is FERMARSI E DARE PRECEDENZA — a stop sign — but one of its
    statements says it stands at an intersection with a road holding the
    DIRITTO DI PRECEDENZA.

Substring matching cannot tell "this sign **is** X" from "this sign is **preceded
by** X" or "**faces** X", and the statements reference neighbouring signs constantly.
Measured: the hint named the wrong sign on 2 of 3 clusters and the model followed it,
turning two correct explanations into confidently wrong ones. Without any hint the
model identified both correctly from the article text on its own.

The figure is the ground truth and all 409 are on disk, so the real fix is to send
the image to a multimodal model. Until then, no hint: `articles.signs_in` still uses
the same matches to *order* the article context, where being wrong costs some
irrelevant statute rather than a wrong answer.

THE OTHER GATES
---------------
  · Any explanation containing a number or a unit (km/h, m, g/l, anni, €, %) is
    flagged. Numeric claims are where models drift, and a wrong speed limit is the
    worst thing this product could say.
  · A model that reports the articles do not settle the question writes nothing at
    all. §3.3: an absent explanation is acceptable, a confidently wrong one is not.
    `entitlement.py` already distinguishes "nobody wrote this" from "pay for it".

Only `approved` is ever served, and approval only happens in the step-7 review
loop. Everything this script writes is `draft` or `flagged`.

Usage:
    python content/generate.py --topic "Segnali di precedenza" --dry-run
    python content/generate.py --topic "Segnali di precedenza" --limit 20
    python content/generate.py --topic "Segnali di precedenza"
"""

from __future__ import annotations

import argparse
import collections
import csv
import json
import re
import sys
import textwrap
from pathlib import Path

from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from api.models import Cluster, Explanation, Question, Topic  # noqa: E402
from articles import (  # noqa: E402
    LABEL,
    articles_for,
    cite,
    load_corpus,
    sign_index,
    signs_in,
)
from shared.config import CONTENT_OUT, settings  # noqa: E402
from shared.constants import LANG_IT, STATUS_APPROVED, STATUS_DRAFT, STATUS_FLAGGED  # noqa: E402
from shared.db import sync_session_factory  # noqa: E402

REPORT_CSV = CONTENT_OUT / "generate_report.csv"

# The Windows console is cp1252, and this script prints model output to it. Italian
# accents and em-dashes happen to be representable; a stray emoji or Greek letter is
# not, and an unhandled UnicodeEncodeError mid-loop would abandon every API call paid
# for up to that point. Degrade the character, never the run.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

# Indicative only, EUR per million tokens, for the "is this affordable" question.
# Wrong prices give a wrong estimate; they never change what gets written.
PRICES = {
    "gpt-5": (1.15, 9.20),
    "gpt-5-mini": (0.23, 1.84),
    "gpt-4o": (2.30, 9.20),
    "gpt-4o-mini": (0.14, 0.55),
}

# Numbers, and the units the ministerial statements actually use. A bare integer
# counts: "tre mesi" is prose, "3 mesi" is a claim someone must check.
NUMERIC_RE = re.compile(
    r"\d|\b(?:km/h|m/s|g/l|km|metri|metro|tonnellate|anni|anno|mesi|mese|"
    r"giorni|per\s?cento|euro)\b",
    re.I,
)

# Article and figure references are citations, not claims — and the prompt *requires*
# the explanation to cite its article, so every draft contains at least one. Left in,
# they matched the `\d` above and the numeric gate fired on all of them: a gate that
# flags 100% of rows carries no information and trains the reviewer to ignore it.
# Observed on the first real run: "Il segnale FERMARSI E DARE PRECEDENZA (art. 107
# Reg.) …" was flagged for a number, having none.
CITATION_RE = re.compile(
    r"art(?:icolo)?\.?\s*\d+[a-z\-]*(?:\s*,?\s*comma\s*\d+[a-z\-]*)?"
    r"|fig(?:ura)?\.?\s*[IVX]+\.\s*\d+[a-z]?",
    re.I,
)

SYSTEM_PROMPT = """\
Sei un esperto di diritto della circolazione stradale italiano.

Ti vengono forniti il testo integrale di uno o più articoli del Codice della Strada
o del suo Regolamento di esecuzione, e alcune affermazioni tratte dal listato
ufficiale dei quiz per la patente di guida.

Svolgi i compiti in questo ordine:

1. Per OGNI affermazione, decidi se è VERA o FALSA basandoti ESCLUSIVAMENTE sugli
   articoli forniti. Non usare conoscenze che non siano nel testo fornito. Non ti
   viene detto quale sia la risposta corretta: devi ricavarla dagli articoli.

2. Scrivi una spiegazione in italiano di DUE frasi, al massimo tre, che esponga la
   REGOLA che sta sotto a queste affermazioni, e che citi l'articolo fra parentesi
   (per esempio "(art. 148 C.d.S.)"). Spiega la regola, non le singole
   affermazioni: la stessa spiegazione deve servire per tutte.

Se gli articoli forniti non bastano a decidere, imposta "insufficiente": true e
lascia "spiegazione" vuota. NON inventare e NON tirare a indovinare: una
spiegazione assente è accettabile, una spiegazione sbagliata non lo è.

Rispondi SOLO con un oggetto JSON di questa forma esatta:

{"insufficiente": false,
 "spiegazione": "...",
 "articolo_citato": "art. 148 C.d.S.",
 "verdetti": [{"n": 1, "risposta": "VERO", "certezza": "alta"}]}

"risposta" è "VERO" o "FALSO". "certezza" è "alta", "media" o "bassa". Includi un
verdetto per ogni affermazione, con "n" pari al suo numero.
"""


def sample_statements(members: list[dict], cap: int) -> list[dict]:
    """Up to `cap` statements, keeping both answers represented.

    A 34-member cluster does not need all 34 judged to detect a bad explanation,
    but judging only the VERO ones would miss exactly the case where the model has
    learnt to agree with whatever it is shown.
    """
    if len(members) <= cap:
        return sorted(members, key=lambda m: m["id"])
    true_ = [m for m in sorted(members, key=lambda m: m["id"]) if m["answer"]]
    false_ = [m for m in sorted(members, key=lambda m: m["id"]) if not m["answer"]]
    out: list[dict] = []
    while len(out) < cap and (true_ or false_):
        for pool in (true_, false_):
            if pool and len(out) < cap:
                out.append(pool.pop(0))
    return sorted(out, key=lambda m: m["id"])


def grounding(topic_name: str, statements: list[str], corpus, index, budget: int) -> list[dict]:
    """The articles to put in front of the model, most specific first.

    Sign-name hits come first because they name the exact article defining the sign
    in question; the topic's hand-mapped articles follow as the floor. Truncated to
    a character budget — a 25-article topic run through in full would be mostly
    irrelevant text, and irrelevant statute is how a model ends up citing the wrong
    comma.
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


def build_user_prompt(cluster_topic: str, judged: list[dict], grounded: list[dict]) -> str:
    """No attempt is made to tell the model which sign the figure shows. Tried, measured,
    reverted — see NAMING THE SIGN in the module docstring."""
    parts = [f"ARGOMENTO MINISTERIALE: {cluster_topic}", "", "ARTICOLI DI LEGGE:"]
    for article in grounded:
        heading = f"{cite(article['source'], article['number'])}"
        rubric = f" — {article['rubric']}" if article["rubric"] else ""
        parts += ["", f"[{LABEL[article['source']]}] {heading}{rubric}", article["text"]]
    parts += ["", "AFFERMAZIONI DA VALUTARE:"]
    parts += [f"{i}. {m['statement']}" for i, m in enumerate(judged, 1)]
    return "\n".join(parts)


def ask(client, model: str, user_prompt: str) -> tuple[dict, tuple[int, int]]:
    """One call. Returns the parsed object and (prompt tokens, completion tokens)."""
    kwargs = dict(
        model=model,
        messages=[{"role": "system", "content": SYSTEM_PROMPT},
                  {"role": "user", "content": user_prompt}],
        response_format={"type": "json_object"},
    )
    try:
        # Legal reasoning should not vary run to run. Not every model family accepts
        # the parameter any more, so it is an attempt rather than a requirement.
        response = client.chat.completions.create(temperature=0, **kwargs)
    except Exception as exc:  # noqa: BLE001
        if "temperature" not in str(exc):
            raise
        response = client.chat.completions.create(**kwargs)

    usage = response.usage
    return (
        json.loads(response.choices[0].message.content),
        (usage.prompt_tokens, usage.completion_tokens),
    )


def is_fatal(exc: Exception) -> bool:
    """Will this fail identically for every remaining cluster?

    A rate limit or a dropped connection is worth skipping past — the cluster has no
    explanation row, so the next run picks it up. A bad key, a model the account
    cannot reach, or an exhausted quota is not: it fails 3382 times in a row and
    buries the one line that says why under 3382 copies of itself.
    """
    if type(exc).__name__ in ("AuthenticationError", "PermissionDeniedError", "NotFoundError"):
        return True
    text = str(exc)
    return any(marker in text for marker in (
        "invalid_api_key", "insufficient_quota", "model_not_found",
        "does not exist or you do not have access",
    ))


def check(parsed: dict, judged: list[dict]) -> tuple[str, list[str], list[dict]]:
    """The gates. Returns (status, reasons, per-statement disagreements)."""
    reasons: list[str] = []
    verdicts = {v.get("n"): str(v.get("risposta", "")).upper()
                for v in parsed.get("verdetti", []) if isinstance(v, dict)}

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

    if disagreements:
        reasons.append(f"argues against the stored answer on "
                       f"{len(disagreements)}/{len(judged)} statements")
    prose = CITATION_RE.sub("", parsed.get("spiegazione") or "")
    if NUMERIC_RE.search(prose):
        reasons.append("contains a number or a unit")
    if any(str(v.get("certezza", "")).lower() == "bassa"
           for v in parsed.get("verdetti", []) if isinstance(v, dict)):
        reasons.append("model reports low confidence")

    return (STATUS_FLAGGED if reasons else STATUS_DRAFT), reasons, disagreements


def load_clusters(session, topic: str | None, cluster_ids: list[int] | None) -> dict[int, dict]:
    query = (
        select(Cluster.id, Cluster.topic_id, Topic.name, Question.id,
               Question.statement_it, Question.answer)
        .join(Topic, Topic.id == Cluster.topic_id)
        .join(Question, Question.cluster_id == Cluster.id)
    )
    if topic:
        query = query.where(Topic.name.like(f"{topic}%"))
    if cluster_ids:
        query = query.where(Cluster.id.in_(cluster_ids))

    out: dict[int, dict] = {}
    for cid, _tid, topic_name, qid, statement, answer in session.execute(query):
        entry = out.setdefault(cid, {"id": cid, "topic": topic_name, "members": []})
        entry["members"].append({"id": qid, "statement": statement, "answer": answer})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--topic", help="ministerial topic name, or a prefix of one")
    ap.add_argument("--cluster", type=int, nargs="*", help="specific cluster ids")
    ap.add_argument("--lang", default=LANG_IT, help="only 'it' is generated from statute")
    ap.add_argument("--model", default=settings.openai_model)
    ap.add_argument("--limit", type=int, help="stop after this many clusters")
    ap.add_argument("--max-statements", type=int, default=12,
                    help="statements judged per cluster")
    ap.add_argument("--context-chars", type=int, default=24000,
                    help="character budget for the article text in one prompt")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the first prompt and the plan, spend nothing")
    ap.add_argument("--regenerate", action="store_true",
                    help="replace existing drafts; approved rows are never touched")
    args = ap.parse_args()

    if args.lang != LANG_IT:
        print("ERROR: only Italian is generated from statute. Translations of the "
              "APPROVED Italian are content/translate.py's job (plan §3.3).",
              file=sys.stderr)
        return 2
    if not args.topic and not args.cluster:
        print("ERROR: pass --topic or --cluster. Generating the whole bank in one go "
              "is 3382 clusters and gates nothing — §3.3 releases per topic.",
              file=sys.stderr)
        return 2

    corpus = load_corpus()
    index = sign_index(corpus)
    factory = sync_session_factory()

    with factory() as session:
        clusters = load_clusters(session, args.topic, args.cluster)
        if not clusters:
            print("no clusters matched — has content/cluster.py --write been run?",
                  file=sys.stderr)
            return 2

        existing = {
            e.cluster_id: e for e in session.scalars(
                select(Explanation).where(Explanation.lang == args.lang)
            )
        }

        todo, skipped = [], collections.Counter()
        for cid in sorted(clusters):
            current = existing.get(cid)
            if current is None:
                todo.append(cid)
            elif current.status == STATUS_APPROVED:
                skipped["already approved"] += 1
            elif args.regenerate:
                todo.append(cid)
            else:
                skipped[f"already {current.status}"] += 1

        if args.limit:
            todo = todo[: args.limit]

        topics = sorted({clusters[c]["topic"].split(";")[0] for c in clusters})
        print(f"{len(clusters)} clusters in {len(topics)} topic(s): {', '.join(topics)}")
        for reason, n in sorted(skipped.items()):
            print(f"  skipping {n} — {reason}")
        print(f"  to generate: {len(todo)}   model {args.model}   lang {args.lang}")
        if not todo:
            return 0

        if args.dry_run:
            cid = todo[0]
            entry = clusters[cid]
            judged = sample_statements(entry["members"], args.max_statements)
            grounded = grounding(entry["topic"], [m["statement"] for m in judged],
                                 corpus, index, args.context_chars)
            prompt = build_user_prompt(entry["topic"], judged, grounded)
            print(f"\n--- cluster {cid}: {len(entry['members'])} statements, "
                  f"{len(judged)} judged, {len(grounded)} articles, "
                  f"{len(prompt)} chars of prompt ---")
            print(textwrap.indent(prompt[:4000], "  "))
            if len(prompt) > 4000:
                print(f"  … {len(prompt) - 4000} more characters")
            ungrounded = [c for c in todo if not grounding(
                clusters[c]["topic"],
                [m["statement"] for m in clusters[c]["members"]],
                corpus, index, args.context_chars)]
            print(f"\nclusters with no article at all: {len(ungrounded)}")
            print("dry run — nothing sent, nothing written")
            return 0

        if not settings.openai_api_key:
            print("ERROR: OPENAI_API_KEY is not set", file=sys.stderr)
            return 2

        from openai import OpenAI

        client = OpenAI(api_key=settings.openai_api_key)
        tokens_in = tokens_out = 0
        stats: collections.Counter = collections.Counter()
        rows = []
        fatal: Exception | None = None

        for n, cid in enumerate(todo, 1):
            entry = clusters[cid]
            judged = sample_statements(entry["members"], args.max_statements)
            grounded = grounding(entry["topic"], [m["statement"] for m in judged],
                                 corpus, index, args.context_chars)
            if not grounded:
                stats["no article mapped"] += 1
                rows.append({"cluster_id": cid, "topic": entry["topic"].split(";")[0],
                             "status": "", "reasons": "no article mapped",
                             "explanation": "", "disagreements": ""})
                continue

            try:
                parsed, (pt, ct) = ask(
                    client, args.model,
                    build_user_prompt(entry["topic"], judged, grounded),
                )
            except Exception as exc:  # noqa: BLE001
                # One cluster failing must not lose the ones already written.
                print(f"  [{n}/{len(todo)}] cluster {cid}: {type(exc).__name__} {exc}")
                stats["api error"] += 1
                if is_fatal(exc):
                    fatal = exc
                    print(f"\nERROR: that will fail the same way for every remaining "
                          f"cluster — stopping after {n} of {len(todo)} rather than "
                          f"repeating it {len(todo) - n} more times.", file=sys.stderr)
                    break
                continue
            tokens_in += pt
            tokens_out += ct

            explanation_text = (parsed.get("spiegazione") or "").strip()
            if parsed.get("insufficiente") or not explanation_text:
                stats["model declined — articles insufficient"] += 1
                rows.append({"cluster_id": cid, "topic": entry["topic"].split(";")[0],
                             "status": "", "reasons": "articles insufficient",
                             "explanation": "", "disagreements": ""})
                continue

            status, reasons, disagreements = check(parsed, judged)
            stats[status] += 1
            for reason in reasons:
                stats[f"  flag: {reason.split(' on ')[0]}"] += 1

            # The disagreements go in alongside the reasons: at review time the
            # question is always "which statement, and what did each side say".
            recorded = "; ".join(reasons + [
                f"q{d['question_id']}: key {d['stored']}, model {d['model']}"
                for d in disagreements
            ]) or None

            current = existing.get(cid)
            if current is None:
                session.add(Explanation(cluster_id=cid, lang=args.lang,
                                        text=explanation_text, status=status,
                                        flags=recorded))
            else:
                current.text = explanation_text
                current.status = status
                current.flags = recorded
                current.reviewed_at = None
                current.reviewer = None
            # Per cluster, not once at the end. A crash at cluster 300 of 3382 would
            # otherwise roll back 300 clusters that have already been paid for, and a
            # rerun skips whatever is already written — so partial progress is worth
            # strictly more than a tidy single transaction.
            session.commit()

            rows.append({
                "cluster_id": cid,
                "topic": entry["topic"].split(";")[0],
                "status": status,
                "reasons": "; ".join(reasons),
                "explanation": explanation_text,
                "disagreements": " | ".join(
                    f"q{d['question_id']} key={d['stored']} model={d['model']}"
                    for d in disagreements
                ),
            })
            mark = "!" if status == STATUS_FLAGGED else " "
            print(f"  [{n}/{len(todo)}] {mark} cluster {cid:>5}  "
                  f"{explanation_text[:88]}")

        written = stats[STATUS_DRAFT] + stats[STATUS_FLAGGED]
        session.commit()

        if written:
            print(f"\ncommitted {written} explanations. "
                  f"tokens: {tokens_in} in, {tokens_out} out")
        else:
            print(f"\nnothing written. tokens: {tokens_in} in, {tokens_out} out")
        if args.model in PRICES and written:
            price_in, price_out = PRICES[args.model]
            cost = (tokens_in * price_in + tokens_out * price_out) / 1_000_000
            print(f"  ~EUR {cost:.2f} at {args.model} list prices "
                  f"(indicative; check the current price list)")
            print(f"  ~EUR {cost / written * 3382:.2f} to do all 3382 clusters "
                  f"at this rate")
        for key in sorted(stats):
            print(f"  {key:<44} {stats[key]}")

        if not written:
            # No run log worth keeping, and no advice worth printing about an empty
            # sheet. Overwriting a previous good report with this would be worse.
            if fatal is not None:
                print("\nfix that and re-run — every cluster is still unwritten, so "
                      "nothing needs undoing.", file=sys.stderr)
                return 1
            return 0

        REPORT_CSV.parent.mkdir(parents=True, exist_ok=True)
        with REPORT_CSV.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=[
                "cluster_id", "topic", "status", "reasons", "explanation",
                "disagreements",
            ])
            writer.writeheader()
            writer.writerows(rows)
        print(f"\n  run log -> {REPORT_CSV}")
        print("  Then: python content/review_export.py --topic ... for the review sheet.")
        if fatal is not None:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
