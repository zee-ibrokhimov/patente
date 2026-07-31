"""Generate the same explanations with two models and compare, without touching the database.

Explanations run gpt-4o; translations run gpt-5-mini. The question is whether the cheaper,
newer model is good enough for explanations too — it is the single largest per-call cost in
the product, because the statute goes in the prompt.

WHAT THIS DOES AND DOES NOT DO

It builds exactly the prompt `explanations.generate` would build, for the same clusters, and
calls each model with it. It runs the SAME quality gates over each reply. It writes nothing
to the database — no cached rows, no events, no cost to the running product — because the
point is to decide, not to switch.

The numbers it produces are the ones that can be counted:

  · declined      — the model said the articles were insufficient. Partly run-to-run noise,
                    which is why the sample is not three clusters.
  · flagged       — tripped a cluster-level gate. Not the same as "wrong", but it is what
                    decides whether a learner sees anything at all.
  · disputes      — disagreed with the ministerial answer key. The most interesting signal:
                    it usually means the model reasoned differently, and occasionally that
                    the key is wrong.
  · tokens, seconds, and the estimated bill.

What it cannot count is whether the ITALIAN IS ANY GOOD, which is the thing that actually
matters. That needs reading, so the replies are written out in full for a blind comparison.

Usage:  python content/compare_models.py [n_clusters] [--out FILE]
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import random
import sys
import time

from sqlalchemy import func, select

from api.models import Question
from api.services import explanations
from shared.constants import LANG_IT
from shared.db import async_session_factory

# The two candidates. A is what explanations use today; B is what translations use.
MODEL_A = "gpt-4o"
MODEL_B = "gpt-5-mini"

# Set for the head-to-head: bound gpt-5-mini's reasoning so the two are compared
# at usable latency rather than one of them thinking for half a minute.
import os
EFFORT_B = os.environ.get("EFFORT_B", "minimal")

# Rough public per-1M-token prices, only to give the comparison an order of magnitude.
# Wrong prices give a wrong ratio, so treat the euros as indicative and the TOKENS as fact.
PRICE_IN = {"gpt-4o": 2.50, "gpt-5-mini": 0.25}
PRICE_OUT = {"gpt-4o": 10.00, "gpt-5-mini": 2.00}

# 30,000 TPM on this account, and one call carries ~8k tokens of statute. Two models per
# cluster means pausing between clusters or the account starts refusing.
PAUSE = 5.0


async def pick_clusters(session, n: int) -> list[int]:
    """A spread: half with a figure, half without, so the vision path is exercised.

    Sign clusters are where the previous model comparison was decided (STATUS §12: guessing
    the sign from the wording was wrong on 2 of 3 clusters), so a sample without them would
    measure the easy half of the problem.
    """
    with_figure = list(await session.scalars(
        select(Question.cluster_id)
        .where(Question.cluster_id.is_not(None), Question.image_path.is_not(None))
        .group_by(Question.cluster_id)
        .order_by(func.random())
        .limit(n - n // 2)
    ))
    without = list(await session.scalars(
        select(Question.cluster_id)
        .where(Question.cluster_id.is_not(None), Question.image_path.is_(None))
        .group_by(Question.cluster_id)
        .order_by(func.random())
        .limit(n // 2)
    ))
    picked = with_figure + without
    random.shuffle(picked)
    return picked


async def one(session, cluster_id: int, model: str) -> dict:
    """One model, one cluster. Never raises — a failure is a result."""
    corpus, index = explanations.corpus_and_index()
    topic, members, image = await explanations.cluster_members(session, cluster_id)
    if not members:
        return {"outcome": "no-members"}

    judged = explanations.sample_statements(members)
    grounded = explanations.select_articles(
        topic, [m["statement"] for m in judged], corpus, index)
    if not grounded:
        return {"outcome": "no-article", "topic": topic}

    client = explanations.openai_client()
    kwargs = dict(
        model=model,
        messages=explanations.build_messages(topic, judged, grounded, image),
        response_format={"type": "json_object"},
    )
    if model == MODEL_B and EFFORT_B:
        kwargs["reasoning_effort"] = EFFORT_B

    started = time.monotonic()
    try:
        try:
            response = await client.chat.completions.create(temperature=0, **kwargs)
        except Exception as exc:                                  # noqa: BLE001
            if "temperature" not in str(exc):
                raise
            response = await client.chat.completions.create(**kwargs)
    except Exception as exc:                                      # noqa: BLE001
        return {"outcome": "error", "detail": f"{type(exc).__name__}: {exc}",
                "seconds": round(time.monotonic() - started, 2)}

    seconds = round(time.monotonic() - started, 2)
    usage = response.usage
    tokens_in, tokens_out = (usage.prompt_tokens, usage.completion_tokens) if usage else (0, 0)

    try:
        parsed = json.loads(response.choices[0].message.content)
    except Exception as exc:                                      # noqa: BLE001
        return {"outcome": "unparseable", "detail": str(exc), "seconds": seconds,
                "tokens_in": tokens_in, "tokens_out": tokens_out}

    texts = explanations.parsed_texts(parsed)
    if parsed.get("insufficiente") or LANG_IT not in texts:
        return {"outcome": "declined", "seconds": seconds,
                "tokens_in": tokens_in, "tokens_out": tokens_out}

    status, reasons, disagreements = explanations.check_gates(parsed, judged)
    return {
        "outcome": "stored",
        "status": status,
        "reasons": reasons,
        "disputes": len(disagreements),
        "judged": len(judged),
        "langs": sorted(texts),
        "texts": texts,
        "sign": parsed.get("segnale_riconosciuto", ""),
        "article": parsed.get("articolo_citato", ""),
        "seconds": seconds,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
    }


def summarise(rows: list[dict], model: str) -> dict:
    ok = [r for r in rows if r["outcome"] == "stored"]
    tokens_in = sum(r.get("tokens_in", 0) for r in rows)
    tokens_out = sum(r.get("tokens_out", 0) for r in rows)
    return {
        "model": model,
        "calls": len(rows),
        "usable": len(ok),
        "declined": sum(1 for r in rows if r["outcome"] == "declined"),
        "errors": sum(1 for r in rows if r["outcome"] in ("error", "unparseable")),
        "draft": sum(1 for r in ok if r["status"] == "draft"),
        "flagged": sum(1 for r in ok if r["status"] == "flagged"),
        "disputes": sum(r["disputes"] for r in ok),
        "statements_judged": sum(r["judged"] for r in ok),
        "all_four_languages": sum(1 for r in ok if len(r["langs"]) == 4),
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "seconds_avg": round(sum(r.get("seconds", 0) for r in rows) / max(1, len(rows)), 2),
        "eur_est": round(
            tokens_in / 1e6 * PRICE_IN.get(model, 0)
            + tokens_out / 1e6 * PRICE_OUT.get(model, 0), 4),
    }


async def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 15
    out = pathlib.Path(
        sys.argv[sys.argv.index("--out") + 1] if "--out" in sys.argv
        else "/tmp/model-comparison.json")

    factory = async_session_factory()
    async with factory() as session:
        fixed = os.environ.get("CLUSTERS", "").strip()
        clusters = ([int(x) for x in fixed.split(",")] if fixed
                    else await pick_clusters(session, n))
        print(f"comparing {MODEL_A} vs {MODEL_B} over {len(clusters)} clusters\n")

        results: dict[str, list[dict]] = {MODEL_A: [], MODEL_B: []}
        pairs = []
        for i, cluster_id in enumerate(clusters, 1):
            topic, members, image = await explanations.cluster_members(session, cluster_id)
            row = {"cluster_id": cluster_id, "topic": topic,
                   "figure": bool(image), "members": len(members),
                   "statements": [m["statement"] for m in members[:3]]}
            for model in (MODEL_A, MODEL_B):
                result = await one(session, cluster_id, model)
                results[model].append(result)
                row[model] = result
                await asyncio.sleep(PAUSE / 2)
            pairs.append(row)

            a, b = row[MODEL_A], row[MODEL_B]
            print(f"[{i}/{len(clusters)}] cluster {cluster_id} "
                  f"({'figure' if image else 'text  '}) "
                  f"{MODEL_A}: {a['outcome']}/{a.get('status', '-')} "
                  f"{a.get('seconds', 0)}s | "
                  f"{MODEL_B}: {b['outcome']}/{b.get('status', '-')} {b.get('seconds', 0)}s")

    summary = {m: summarise(rows, m) for m, rows in results.items()}
    out.write_text(json.dumps({"summary": summary, "pairs": pairs},
                              ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 68)
    for model in (MODEL_A, MODEL_B):
        s = summary[model]
        print(f"\n{model}")
        print(f"  usable {s['usable']}/{s['calls']}   declined {s['declined']}   "
              f"errors {s['errors']}")
        print(f"  draft {s['draft']}   flagged {s['flagged']}   "
              f"all four languages {s['all_four_languages']}/{s['usable']}")
        print(f"  disputed {s['disputes']} of {s['statements_judged']} statements judged")
        print(f"  {s['tokens_in']} in + {s['tokens_out']} out   "
              f"~EUR {s['eur_est']}   {s['seconds_avg']}s avg")
    a, b = summary[MODEL_A], summary[MODEL_B]
    if b["eur_est"]:
        print(f"\ncost ratio: {MODEL_A} is {a['eur_est'] / b['eur_est']:.1f}x "
              f"{MODEL_B} on this sample")
    print(f"\nfull replies written to {out} — the Italian still has to be READ")


asyncio.run(main())
