"""
generate.py — produce explanations for a whole topic in advance.

No longer the only path, and no longer the important one. Explanations are generated on
request and cached (STATUS.md §13), so this is a **pre-warmer**: run it over a topic
before launch or before a demo, and the first real user gets a cached answer instead of
a ten-second wait. Every gate, prompt and decision lives in
`api/services/explanations.py`, which is what the API calls at runtime — this file must
never grow a second opinion about what a servable explanation is.

It is also where you measure. Running a topic through here gives the flag rate, the
decline rate and the real cost per cluster, which is the M2 number the schedule was
guessed from, and it does it without a user waiting.

Usage:
    python content/generate.py --topic "Segnali di precedenza" --dry-run
    python content/generate.py --topic "Segnali di precedenza" --limit 20
    python content/generate.py --cluster 623 624 625 --model gpt-4o
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import csv
import sys
import textwrap
from pathlib import Path

from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.models import Cluster, Explanation, Topic  # noqa: E402
from api.services import explanations  # noqa: E402
from shared.config import CONTENT_OUT, settings  # noqa: E402
from shared.constants import LANG_IT, SERVABLE_STATUSES, STATUS_APPROVED  # noqa: E402
from shared.db import async_session_factory  # noqa: E402

REPORT_CSV = CONTENT_OUT / "generate_report.csv"

# The Windows console is cp1252 and this prints model output to it. Italian accents are
# representable; a stray emoji is not, and an unhandled UnicodeEncodeError mid-run would
# abandon the API calls already paid for. Degrade the character, never the run.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

# Indicative only, EUR per million tokens, for the "is this affordable" question.
PRICES = {
    "gpt-5": (1.15, 9.20),
    "gpt-5-mini": (0.23, 1.84),
    "gpt-4o": (2.30, 9.20),
    "gpt-4o-mini": (0.14, 0.55),
}


async def clusters_to_do(session, topic, cluster_ids, regenerate):
    """(todo cluster ids, {reason: count} for what was skipped, total matched)."""
    query = select(Cluster.id, Topic.name).join(Topic, Topic.id == Cluster.topic_id)
    if topic:
        query = query.where(Topic.name.like(f"{topic}%"))
    if cluster_ids:
        query = query.where(Cluster.id.in_(cluster_ids))
    matched = {cid: name for cid, name in (await session.execute(query)).all()}

    stored = {
        row.cluster_id: row.status
        for row in (await session.scalars(
            select(Explanation).where(Explanation.lang == LANG_IT)
        )).all()
    }

    todo, skipped = [], collections.Counter()
    for cid in sorted(matched):
        status = stored.get(cid)
        if status is None:
            todo.append(cid)
        elif status == STATUS_APPROVED:
            skipped["already approved — never overwritten"] += 1
        elif regenerate:
            todo.append(cid)
        else:
            skipped[f"already {status}"] += 1
    return todo, skipped, matched


async def show_prompt(session, cluster_id):
    corpus, index = explanations.corpus_and_index()
    topic, members, image = await explanations.cluster_members(session, cluster_id)
    judged = explanations.sample_statements(members)
    grounded = explanations.select_articles(
        topic, [m["statement"] for m in judged], corpus, index
    )
    text = explanations.build_text_prompt(topic, judged, grounded)
    print(f"\n--- cluster {cluster_id}: {len(members)} statements, {len(judged)} judged, "
          f"{len(grounded)} articles, figure={image or 'none'}, "
          f"{len(text)} chars of text prompt ---")
    print(textwrap.indent(text[:3500], "  "))
    if len(text) > 3500:
        print(f"  … {len(text) - 3500} more characters")
    if image:
        part = explanations.figure_part(image)
        size = len(part["image_url"]["url"]) if part else 0
        print(f"\n  + the figure is attached: {image} ({size} chars of data URL)")
    else:
        print("\n  no figure — a text-only cluster")


async def run(args) -> int:
    factory = async_session_factory()
    async with factory() as session:
        todo, skipped, matched = await clusters_to_do(
            session, args.topic, args.cluster, args.regenerate
        )
        topics = sorted({name.split(";")[0] for name in matched.values()})
        print(f"{len(matched)} clusters in {len(topics)} topic(s): {', '.join(topics)}")
        for reason, n in sorted(skipped.items()):
            print(f"  skipping {n} — {reason}")
        if args.limit:
            todo = todo[: args.limit]
        print(f"  to generate: {len(todo)}   model {args.model or settings.openai_model}")
        if not todo:
            return 0

        if args.dry_run:
            await show_prompt(session, todo[0])
            print("\ndry run — nothing sent, nothing written")
            return 0

        tokens_in = tokens_out = 0
        stats: collections.Counter = collections.Counter()
        rows = []
        fatal = None

        for n, cid in enumerate(todo, 1):
            result = await explanations.generate(session, cid, args.model)
            tokens_in += result.tokens_in
            tokens_out += result.tokens_out
            stats[result.outcome] += 1

            row = result.row
            if row is not None:
                stats[row.status] += 1
                mark = " " if row.status in SERVABLE_STATUSES else "!"
                print(f"  [{n}/{len(todo)}] {mark} cluster {cid:>5}  {row.text[:86]}")
            else:
                print(f"  [{n}/{len(todo)}]   cluster {cid:>5}  ({result.outcome}"
                      f"{': ' + result.detail if result.detail else ''})")

            rows.append({
                "cluster_id": cid,
                "topic": matched[cid].split(";")[0],
                "outcome": result.outcome,
                "status": row.status if row else "",
                "flags": (row.flags or "") if row else result.detail,
                "explanation": row.text if row else "",
            })

            if result.fatal:
                fatal = result
                print(f"\nERROR: that will fail the same way for every remaining "
                      f"cluster — stopping after {n} of {len(todo)}.", file=sys.stderr)
                break

        written = stats["stored"]
        print(f"\n{written} stored. tokens: {tokens_in} in, {tokens_out} out")
        if args.model in PRICES or (not args.model and settings.openai_model in PRICES):
            name = args.model or settings.openai_model
            price_in, price_out = PRICES[name]
            cost = (tokens_in * price_in + tokens_out * price_out) / 1_000_000
            print(f"  ~EUR {cost:.2f} at {name} list prices (indicative)")
            if written:
                total = await session.scalar(select(Cluster.id).order_by(Cluster.id.desc()))
                print(f"  ~EUR {cost / written * (total or 3382):.2f} for the whole bank "
                      f"at this rate")
        for key in sorted(stats):
            print(f"  {key:<44} {stats[key]}")

        if not written:
            return 1 if fatal else 0

        REPORT_CSV.parent.mkdir(parents=True, exist_ok=True)
        with REPORT_CSV.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=[
                "cluster_id", "topic", "outcome", "status", "flags", "explanation",
            ])
            writer.writeheader()
            writer.writerows(rows)
        print(f"\n  run log -> {REPORT_CSV}")
        print("  Then: python content/review_export.py --topic ... for the review sheet.")
        return 1 if fatal else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--topic", help="ministerial topic name, or a prefix of one")
    ap.add_argument("--cluster", type=int, nargs="*", help="specific cluster ids")
    ap.add_argument("--model", default=None, help="defaults to OPENAI_MODEL in .env")
    ap.add_argument("--limit", type=int, help="stop after this many clusters")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the first prompt and the plan, spend nothing")
    ap.add_argument("--regenerate", action="store_true",
                    help="replace existing drafts; approved rows are never touched")
    args = ap.parse_args()

    if not args.topic and not args.cluster:
        print("ERROR: pass --topic or --cluster. Explanations are generated on request "
              "now, so a whole-bank run is a pre-warm, not a prerequisite — do it a "
              "topic at a time (§3.3 releases per topic).", file=sys.stderr)
        return 2

    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
