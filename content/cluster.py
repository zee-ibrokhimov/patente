"""
cluster.py — group the 7106 statements into rule clusters.

Build step 6, first half (plan §14.4). No LLM: near-identical statements are found
by string similarity, which is deterministic, free, and re-runnable.

WHY THIS RUNS BEFORE ANY GENERATION
-----------------------------------
A large share of the bank is the same rule reworded. Writing one explanation per
*rule* instead of per statement cuts the volume dramatically and means a
correction lands in one row (§3.3). It also produces the number the whole project
hangs on: §11 says the real cost is "minutes per explanation × cluster count", and
that multiplier decides whether this is a three-month or a twelve-month project.
Until it is measured, the M2 and §13 kill criteria are undecidable.

CLUSTERING IS WITHIN A TOPIC, NEVER ACROSS
------------------------------------------
Explanations key off cluster_id and §3.3 releases content one topic at a time. A
cluster spanning two topics would mean neither could go live independently, so
the topic is a hard boundary even where the same rule genuinely recurs.

TWO STRATEGIES, REPORTED SIDE BY SIDE
-------------------------------------
  text    similarity over the statement wording alone.

  figure  statements sharing a figure are about the same sign, so the figure is
          the cluster key; the remaining text-only statements fall back to
          similarity. Sign-recognition questions are a large share of the bank
          and 409 distinct figures serve 3946 statements, so this collapses far
          harder — at the risk of an explanation so generic it stops being worth
          paying for, which is the whole product.

--report prints both and writes nothing, because which to adopt is a judgement
about explanation quality, not a fact about the data.

Usage:
    python content/cluster.py --report
    python content/cluster.py --report --sample     # + an HTML page of examples
    python content/cluster.py --strategy figure --write
"""

from __future__ import annotations

import argparse
import collections
import html
import re
import sys
import unicodedata
from pathlib import Path

from rapidfuzz import fuzz, process
from sqlalchemy import select, update

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.models import Cluster, Question, Topic  # noqa: E402
from shared.config import CONTENT_OUT  # noqa: E402
from shared.db import sync_session_factory  # noqa: E402

DEFAULT_THRESHOLD = 88
STOPWORDS = {"il", "lo", "la", "i", "gli", "le", "un", "uno", "una", "di", "a", "da",
             "in", "con", "su", "per", "tra", "fra", "e", "che", "del", "della", "dei",
             "delle", "al", "alla", "ai", "alle", "dal", "dalla", "nel", "nella", "si"}


def normalise(text: str) -> str:
    """Fold away everything that is wording rather than rule.

    Accents are stripped only for comparison — the stored ministerial text is
    never touched.
    """
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^\w\s]", " ", text)
    tokens = [w for w in text.split() if w not in STOPWORDS]
    return " ".join(tokens)


def connected_components(n: int, edges) -> list[list[int]]:
    """Union-find. Similarity is not transitive, so components chain: A~B and B~C
    pull A and C together even when they are unalike. A high threshold keeps the
    chains short; the report prints the largest clusters so chaining is visible
    rather than assumed away."""
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in edges:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    groups: dict[int, list[int]] = collections.defaultdict(list)
    for i in range(n):
        groups[find(i)].append(i)
    return list(groups.values())


def cluster_by_text(statements: list[str], threshold: int) -> list[list[int]]:
    if not statements:
        return []
    if len(statements) == 1:
        return [[0]]

    keys = [normalise(s) for s in statements]
    scores = process.cdist(keys, keys, scorer=fuzz.token_sort_ratio, workers=-1)
    edges = [
        (i, j)
        for i in range(len(keys))
        for j in range(i + 1, len(keys))
        if scores[i][j] >= threshold
    ]
    return connected_components(len(keys), edges)


def build_clusters(rows: list[dict], strategy: str, threshold: int) -> dict[int, list[dict]]:
    """rows -> {cluster index: [question rows]}. Topic is always a hard boundary."""
    clusters: dict[int, list[dict]] = {}
    next_id = 0

    by_topic: dict[int, list[dict]] = collections.defaultdict(list)
    for row in rows:
        by_topic[row["topic_id"]].append(row)

    for topic_id in sorted(by_topic):
        members = by_topic[topic_id]

        if strategy == "figure":
            with_figure: dict[str, list[dict]] = collections.defaultdict(list)
            without: list[dict] = []
            for row in members:
                (with_figure[row["image"]].append(row) if row["image"]
                 else without.append(row))
            for _path, group in sorted(with_figure.items()):
                clusters[next_id] = group
                next_id += 1
            members = without

        if not members:
            continue
        for component in cluster_by_text([r["statement"] for r in members], threshold):
            clusters[next_id] = [members[i] for i in component]
            next_id += 1

    return clusters


def load_rows(session) -> list[dict]:
    rows = session.execute(
        select(Question.id, Question.topic_id, Question.statement_it,
               Question.image_path, Question.answer, Topic.name)
        .join(Topic, Topic.id == Question.topic_id)
    ).all()
    return [
        {"id": qid, "topic_id": tid, "statement": text, "image": image,
         "answer": answer, "topic": topic}
        for qid, tid, text, image, answer, topic in rows
    ]


def report(rows: list[dict], clusters: dict[int, list[dict]], label: str) -> dict:
    sizes = sorted((len(v) for v in clusters.values()), reverse=True)
    singletons = sum(1 for s in sizes if s == 1)
    total = len(rows)
    n = len(clusters)

    print(f"\n  {label}")
    print(f"    clusters          : {n}")
    print(f"    reduction         : {total / n:.1f}x  ({total} statements)")
    print(f"    singletons        : {singletons}  ({singletons / n:.0%} of clusters)")
    print(f"    median / largest  : {sizes[len(sizes) // 2]} / {sizes[0]}")
    for minutes in (3, 5):
        hours = n * minutes / 60
        print(f"    review at {minutes} min/cluster : {hours:.0f} h"
              f"  ({hours / 3:.0f} evenings at 3 h)")
    return {"clusters": n, "singletons": singletons, "sizes": sizes}


def per_topic_table(clusters: dict[int, list[dict]]) -> None:
    stats: dict[str, list[int]] = collections.defaultdict(lambda: [0, 0])
    for members in clusters.values():
        topic = members[0]["topic"]
        stats[topic][0] += 1
        stats[topic][1] += len(members)

    print(f"\n    {'clusters':>8} {'stmts':>7} {'ratio':>6}  topic")
    for topic, (n_clusters, n_statements) in sorted(
        stats.items(), key=lambda kv: kv[1][0]
    ):
        short = topic.split(";")[0].split(" - ")[0][:66]
        print(f"    {n_clusters:>8} {n_statements:>7} {n_statements / n_clusters:>5.1f}x  {short}")


def write_sample(clusters: dict[int, list[dict]], path: Path, limit: int = 40) -> None:
    """An HTML page of real clusters. Cluster quality is a judgement call, and it
    cannot be made from a size histogram."""
    ordered = sorted(clusters.values(), key=len, reverse=True)
    mid = len(ordered) // 2
    chosen = ordered[:12] + ordered[mid : mid + 14] + [c for c in ordered if len(c) == 1][:14]

    css = """body{font:15px/1.55 -apple-system,Segoe UI,Roboto,sans-serif;margin:0 auto;
    padding:24px;max-width:980px;background:#fafafa;color:#1a1a1a}
    h1{font-size:21px;margin:0 0 4px}p.sub{color:#666;margin:0 0 20px;font-size:14px}
    .c{background:#fff;border-radius:7px;padding:13px 16px;margin:0 0 13px;
       box-shadow:0 1px 3px rgba(0,0,0,.08)}
    .h{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:#777;
       margin-bottom:8px;display:flex;gap:10px;align-items:center}
    .n{background:#eef1f5;border-radius:11px;padding:1px 9px;font-weight:600;color:#374}
    .s{padding:3px 0;display:flex;gap:9px}
    .a{font-weight:700;font-size:11.5px;min-width:44px;padding-top:2px}
    .v{color:#0a7d33}.f{color:#c0271c}
    img{width:52px;height:52px;object-fit:contain;float:right;margin-left:12px;
        border:1px solid #e4e4e4;border-radius:4px;background:#fff}
    @media(prefers-color-scheme:dark){body{background:#16181c;color:#e8e8e8}
      .c{background:#22262c;box-shadow:none}.n{background:#2f353d;color:#cfe}}"""

    parts = [f"<!doctype html><meta charset=utf-8><title>Clusters</title><style>{css}</style>",
             "<h1>Cluster sample</h1>",
             "<p class=sub>Largest, median and singleton clusters. Ask of each: would "
             "<b>one</b> explanation genuinely serve every statement here?</p>"]
    for members in chosen[:limit]:
        img = members[0].get("image")
        figure = f'<img src="{img}">' if img else ""
        parts.append(f'<div class=c>{figure}<div class=h><span class=n>{len(members)}</span>'
                     f'<span>{html.escape(members[0]["topic"].split(";")[0][:70])}</span></div>')
        for row in members[:14]:
            mark = "v" if row["answer"] else "f"
            word = "VERO" if row["answer"] else "FALSO"
            parts.append(f'<div class=s><span class="a {mark}">{word}</span>'
                         f'<span>{html.escape(row["statement"])}</span></div>')
        if len(members) > 14:
            parts.append(f'<div class=s><span class=a></span><span>… and '
                         f'{len(members) - 14} more</span></div>')
        parts.append("</div>")
    path.write_text("".join(parts), encoding="utf-8")
    print(f"\n  sample -> {path}")


def persist(session, clusters: dict[int, list[dict]]) -> None:
    session.execute(update(Question).values(cluster_id=None))
    session.query(Cluster).delete()
    session.flush()

    for index, members in enumerate(sorted(clusters.values(), key=lambda m: m[0]["id"]), 1):
        # Longest statement as the provisional summary: it usually carries the
        # most complete phrasing of the rule. generate.py replaces it.
        summary = max((m["statement"] for m in members), key=len)
        session.add(Cluster(id=index, topic_id=members[0]["topic_id"],
                            rule_summary=summary[:300]))
        session.flush()
        session.execute(
            update(Question)
            .where(Question.id.in_([m["id"] for m in members]))
            .values(cluster_id=index)
        )
    session.flush()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", choices=("text", "figure"), default="text")
    ap.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD)
    ap.add_argument("--report", action="store_true", help="compare both, write nothing")
    ap.add_argument("--sample", action="store_true", help="also write an HTML sample")
    ap.add_argument("--write", action="store_true", help="persist clusters to the database")
    args = ap.parse_args()

    factory = sync_session_factory()
    with factory() as session:
        rows = load_rows(session)
        if not rows:
            print("ERROR: no questions in the database — run content/seed.py first",
                  file=sys.stderr)
            return 2
        print(f"loaded {len(rows)} statements across "
              f"{len({r['topic_id'] for r in rows})} topics")

        if args.report:
            print(f"\nthreshold {args.threshold}")
            for strategy in ("text", "figure"):
                built = build_clusters(rows, strategy, args.threshold)
                report(rows, built, f"strategy = {strategy}")
                if strategy == args.strategy:
                    chosen = built
            per_topic_table(chosen)
            if args.sample:
                write_sample(chosen, CONTENT_OUT / "clusters.html")
            print("\nnothing written — re-run with --write to persist")
            return 0

        clusters = build_clusters(rows, args.strategy, args.threshold)
        report(rows, clusters, f"strategy = {args.strategy}")
        if args.sample:
            write_sample(clusters, CONTENT_OUT / "clusters.html")

        if args.write:
            persist(session, clusters)
            session.commit()
            print(f"\ncommitted {len(clusters)} clusters")
        else:
            print("\nnothing written — pass --write to persist")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
