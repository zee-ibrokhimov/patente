"""What would it cost to generate the whole product's AI content, once?

Explanations and translations are both cached forever once produced, so the ceiling is a
ONE-OFF: 3382 clusters and 7106 questions, after which the marginal cost of a learner is
zero. This measures the per-unit cost of each on real content and projects the total.

Writes nothing to the database.

ON THE EUROS. The token counts are measured and are facts. The prices are assumptions and
are stated at the top so they can be corrected — a wrong price gives a wrong total but the
RATIO between options survives, because both sides are priced the same way.

Usage:  python content/measure_costs.py [n_samples]
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys
import time

from sqlalchemy import func, select

from api.models import Question
from api.services import explanations, translations
from shared.constants import LANG_IT
from shared.db import async_session_factory

# EUR per 1M tokens. Assumptions — correct these and rerun rather than trusting them.
PRICES = {
    "gpt-4o": {"in": 2.50, "out": 10.00},
    "gpt-5-mini": {"in": 0.25, "out": 2.00},
}

PAUSE = 3.0


def eur(model: str, tokens_in: int, tokens_out: int) -> float:
    p = PRICES[model]
    return tokens_in / 1e6 * p["in"] + tokens_out / 1e6 * p["out"]


async def measure_translation(session, question, model: str,
                              effort: str | None = None) -> dict:
    """One translation call, priced. Mirrors translations.generate without storing."""
    client = explanations.openai_client()
    # The REAL prompt, not an approximation of it — a hand-rolled version would measure a
    # call the product never makes, and the token count is the whole point here.
    kwargs = dict(
        model=model,
        messages=translations.build_messages(question),
        response_format={"type": "json_object"},
    )
    if effort:
        kwargs["reasoning_effort"] = effort
    started = time.monotonic()
    try:
        try:
            response = await client.chat.completions.create(temperature=0, **kwargs)
        except Exception as exc:                                      # noqa: BLE001
            if "temperature" not in str(exc):
                raise
            response = await client.chat.completions.create(**kwargs)
    except Exception as exc:                                          # noqa: BLE001
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}

    usage = response.usage
    tokens_in, tokens_out = (usage.prompt_tokens, usage.completion_tokens) if usage else (0, 0)
    try:
        parsed = json.loads(response.choices[0].message.content)
        langs = sorted(k for k in parsed if k in ("ru", "en", "uz"))
    except Exception:                                                 # noqa: BLE001
        langs = []
    return {"ok": bool(langs), "langs": langs, "tokens_in": tokens_in,
            "tokens_out": tokens_out, "seconds": round(time.monotonic() - started, 2)}


async def measure_explanation(session, cluster_id: int, model: str, effort: str | None) -> dict:
    corpus, index = explanations.corpus_and_index()
    topic, members, image = await explanations.cluster_members(session, cluster_id)
    if not members:
        return {"ok": False, "detail": "no members"}
    judged = explanations.sample_statements(members)
    grounded = explanations.select_articles(
        topic, [m["statement"] for m in judged], corpus, index)
    if not grounded:
        return {"ok": False, "detail": "no article"}

    client = explanations.openai_client()
    kwargs = dict(
        model=model,
        messages=explanations.build_messages(topic, judged, grounded, image),
        response_format={"type": "json_object"},
    )
    if effort:
        kwargs["reasoning_effort"] = effort

    started = time.monotonic()
    try:
        try:
            response = await client.chat.completions.create(temperature=0, **kwargs)
        except Exception as exc:                                      # noqa: BLE001
            if "temperature" not in str(exc):
                raise
            response = await client.chat.completions.create(**kwargs)
    except Exception as exc:                                          # noqa: BLE001
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}

    usage = response.usage
    tokens_in, tokens_out = (usage.prompt_tokens, usage.completion_tokens) if usage else (0, 0)
    try:
        parsed = json.loads(response.choices[0].message.content)
        usable = not parsed.get("insufficiente") and LANG_IT in explanations.parsed_texts(parsed)
    except Exception:                                                 # noqa: BLE001
        usable = False
    return {"ok": usable, "tokens_in": tokens_in, "tokens_out": tokens_out,
            "seconds": round(time.monotonic() - started, 2)}


async def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 12
    factory = async_session_factory()

    async with factory() as session:
        total_q = await session.scalar(select(func.count(Question.id)))
        total_c = await session.scalar(
            select(func.count(func.distinct(Question.cluster_id)))
            .where(Question.cluster_id.is_not(None)))

        questions = list(await session.scalars(
            select(Question).order_by(func.random()).limit(n)))
        clusters = list(await session.scalars(
            select(Question.cluster_id).where(Question.cluster_id.is_not(None))
            .group_by(Question.cluster_id).order_by(func.random()).limit(n)))

        print(f"bank: {total_q} questions, {total_c} clusters\n")

        # --- translations, on the model they already use --------------------
        tr_units = {}
        for effort in (None, "low", "minimal"):
            name = "gpt-5-mini" + (f" ({effort})" if effort else " (default)")
            print(f"measuring {n} translations on {name} …")
            tr = []
            for q in questions:
                tr.append(await measure_translation(session, q, "gpt-5-mini", effort))
                await asyncio.sleep(PAUSE)
            tr_ok = [r for r in tr if r.get("ok")]
            tr_in = sum(r.get("tokens_in", 0) for r in tr)
            tr_out = sum(r.get("tokens_out", 0) for r in tr)
            tr_units[name] = eur("gpt-5-mini", tr_in, tr_out) / max(1, len(tr))
            print(f"  {len(tr_ok)}/{len(tr)} usable   "
                  f"{tr_in // max(1,len(tr))} in + {tr_out // max(1,len(tr))} out per question   "
                  f"{sum(r.get('seconds',0) for r in tr)/max(1,len(tr)):.1f}s avg")
        tr_unit = tr_units["gpt-5-mini (default)"]

        # --- explanations, three ways ---------------------------------------
        arms = [("gpt-4o", None), ("gpt-5-mini", None), ("gpt-5-mini", "low")]
        ex_unit = {}
        for model, effort in arms:
            name = f"{model}" + (f" ({effort})" if effort else "")
            print(f"\nmeasuring {n} explanations on {name} …")
            rows = []
            for cid in clusters:
                rows.append(await measure_explanation(session, cid, model, effort))
                await asyncio.sleep(PAUSE)
            ok = [r for r in rows if r.get("ok")]
            t_in = sum(r.get("tokens_in", 0) for r in rows)
            t_out = sum(r.get("tokens_out", 0) for r in rows)
            unit = eur(model, t_in, t_out) / max(1, len(rows))
            ex_unit[name] = unit
            print(f"  {len(ok)}/{len(rows)} usable   "
                  f"{t_in // max(1,len(rows))} in + {t_out // max(1,len(rows))} out per cluster   "
                  f"{sum(r.get('seconds',0) for r in rows)/max(1,len(rows)):.1f}s avg")

    print("\n" + "=" * 72)
    print("ONE-OFF COST TO GENERATE THE ENTIRE BANK")
    print("=" * 72)
    tr_total = tr_unit * total_q
    for name, unit in tr_units.items():
        print(f"translations   {total_q} questions x EUR {unit:.5f}  =  "
              f"EUR {unit * total_q:7.2f}   [{name}]")
    print()
    for name, unit in ex_unit.items():
        print(f"explanations   {total_c} clusters  x EUR {unit:.5f}  =  "
              f"EUR {unit * total_c:7.2f}   [{name}]")

    print("\n" + "-" * 72)
    print("TOTAL, translations + explanations, one off:")
    for name, unit in ex_unit.items():
        print(f"  {name:22s}  EUR {tr_total + unit * total_c:7.2f}")
    print("\nBoth are cached permanently, so this is a ceiling, not a running cost.")
    print(f"Prices assumed, EUR per 1M tokens: {json.dumps(PRICES)}")


asyncio.run(main())
