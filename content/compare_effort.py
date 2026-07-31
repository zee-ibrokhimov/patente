"""Does capping gpt-5-mini's reasoning make it fast enough to serve explanations?

The first comparison found gpt-5-mini roughly 3x cheaper than gpt-4o and dramatically
SLOWER — 33s average against gpt-4o's 4.3s, with one cluster at 42.8s against a 45s timeout.
The cause is visible in the token counts: ~5000 completion tokens against ~650, almost all of
it reasoning the learner never sees.

That matters more than it would elsewhere, because explanations are generated in the
FOREGROUND. The owner declined pre-warming on cost, so the call happens when somebody taps
"Why?" and waits. Thirty-five seconds of waiting is not a feature.

`reasoning_effort` bounds that. This runs the same clusters at "low" and "minimal" to see
whether the cost saving survives being made fast enough to use.

Writes nothing to the database. Usage:  python content/compare_effort.py [n] [--out FILE]
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys
import time

import pathlib as _pathlib
import sys as _sys
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent))
from compare_models import PRICE_IN, PRICE_OUT, pick_clusters, summarise
from api.services import explanations
from shared.constants import LANG_IT
from shared.db import async_session_factory

MODEL = "gpt-5-mini"
EFFORTS = ("low", "minimal")
PAUSE = 4.0


async def one(session, cluster_id: int, effort: str) -> dict:
    corpus, index = explanations.corpus_and_index()
    topic, members, image = await explanations.cluster_members(session, cluster_id)
    if not members:
        return {"outcome": "no-members"}

    judged = explanations.sample_statements(members)
    grounded = explanations.select_articles(
        topic, [m["statement"] for m in judged], corpus, index)
    if not grounded:
        return {"outcome": "no-article"}

    client = explanations.openai_client()
    started = time.monotonic()
    try:
        response = await client.chat.completions.create(
            model=MODEL,
            messages=explanations.build_messages(topic, judged, grounded, image),
            response_format={"type": "json_object"},
            reasoning_effort=effort,
        )
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
    return {"outcome": "stored", "status": status, "reasons": reasons,
            "disputes": len(disagreements), "judged": len(judged),
            "langs": sorted(texts), "texts": texts,
            "sign": parsed.get("segnale_riconosciuto", ""),
            "seconds": seconds, "tokens_in": tokens_in, "tokens_out": tokens_out}


async def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 12
    out = pathlib.Path(
        sys.argv[sys.argv.index("--out") + 1] if "--out" in sys.argv
        else "/tmp/effort-comparison.json")

    factory = async_session_factory()
    async with factory() as session:
        clusters = await pick_clusters(session, n)
        print(f"{MODEL} at reasoning_effort {EFFORTS} over {len(clusters)} clusters\n")

        results: dict[str, list[dict]] = {e: [] for e in EFFORTS}
        pairs = []
        for i, cluster_id in enumerate(clusters, 1):
            topic, members, image = await explanations.cluster_members(session, cluster_id)
            row = {"cluster_id": cluster_id, "topic": topic, "figure": bool(image)}
            for effort in EFFORTS:
                result = await one(session, cluster_id, effort)
                results[effort].append(result)
                row[effort] = result
                await asyncio.sleep(PAUSE)
            pairs.append(row)
            print(f"[{i}/{len(clusters)}] {cluster_id} "
                  f"({'figure' if image else 'text  '}) "
                  + "  ".join(
                      f"{e}: {row[e]['outcome']}/{row[e].get('status','-')} "
                      f"{row[e].get('seconds',0)}s" for e in EFFORTS))

    summary = {e: summarise(rows, MODEL) for e, rows in results.items()}
    out.write_text(json.dumps({"summary": summary, "pairs": pairs},
                              ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 68)
    for effort in EFFORTS:
        s = summary[effort]
        print(f"\n{MODEL}  reasoning_effort={effort}")
        print(f"  usable {s['usable']}/{s['calls']}   declined {s['declined']}   "
              f"errors {s['errors']}")
        print(f"  draft {s['draft']}   flagged {s['flagged']}   "
              f"disputed {s['disputes']}/{s['statements_judged']}")
        print(f"  {s['tokens_in']} in + {s['tokens_out']} out   "
              f"~EUR {s['eur_est']}   {s['seconds_avg']}s avg")
    print(f"\nwritten to {out}")


asyncio.run(main())
