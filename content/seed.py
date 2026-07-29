"""
seed.py — load content/out/questions.json into the database.

Build step 2 (plan §14.4). Diff-based from the first run, because the Ministry
reissues the listato periodically and a reseed must not be a rebuild:

  · Topics are keyed by NAME, not by the id extract.py assigns. That id is just
    alphabetical position, so a single new topic in a future listato would
    renumber the rest and silently re-point every question. Names are stable.

  · Figures keep their `telegram_file_id` across reseeds. Losing it would mean
    re-uploading every sign image to every user — the one thing §6.4 says would
    become the entire bandwidth bill.

  · A question whose statement text or answer CHANGED has its `cluster_id`
    cleared. The cluster is what an approved explanation hangs off, so keeping
    the old link would leave an explanation written for wording that no longer
    exists — or, if the answer flipped, one that now argues the opposite of the
    stored key. Re-clustering is cheap; a confidently wrong explanation is not.

  · Retired questions are reported, never deleted, unless --prune is passed.
    Users have progress rows pointing at them.

Usage:
    python content/seed.py --dry-run     # show the diff, write nothing
    python content/seed.py               # apply
    python content/seed.py --prune       # also remove retired questions
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import sys
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.models import Figure, Question, Quesito, Topic  # noqa: E402
from shared.config import CONTENT_OUT, QUESTIONS_JSON  # noqa: E402
from shared.db import sync_session_factory  # noqa: E402


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def collect_figures(data: dict, figure_root: Path = CONTENT_OUT) -> dict[str, str]:
    """Map figure path -> sha256, verifying every referenced file is on disk."""
    paths = {q["image"] for q in data["questions"] if q["image"]}
    missing, out = [], {}
    for rel in sorted(paths):
        f = figure_root / rel
        if not f.exists():
            missing.append(rel)
            continue
        out[rel] = sha256_of(f)
    if missing:
        raise SystemExit(
            f"ERROR: {len(missing)} figures referenced by questions.json are not on "
            f"disk: {missing[:5]} — re-run content/extract.py"
        )
    return out


def seed(
    session: Session,
    data: dict,
    prune: bool = False,
    figure_root: Path = CONTENT_OUT,
) -> tuple[collections.Counter, list[int], list[int]]:
    stats: collections.Counter = collections.Counter()

    # --- topics, keyed by name -------------------------------------------------
    existing_topics = {t.name: t for t in session.scalars(select(Topic))}
    topic_id: dict[str, int] = {}
    next_topic_id = max((t.id for t in existing_topics.values()), default=0) + 1
    for entry in data["topics"]:
        name = entry["name"]
        if name in existing_topics:
            topic_id[name] = existing_topics[name].id
            stats["topic_unchanged"] += 1
        else:
            session.add(Topic(id=next_topic_id, name=name))
            topic_id[name] = next_topic_id
            next_topic_id += 1
            stats["topic_new"] += 1
    session.flush()

    # --- figures, preserving the Telegram file_id cache ------------------------
    figures = collect_figures(data, figure_root)
    existing_figures = {f.path: f for f in session.scalars(select(Figure))}
    for path, digest in figures.items():
        current = existing_figures.get(path)
        if current is None:
            session.add(Figure(path=path, sha256=digest))
            stats["figure_new"] += 1
        elif current.sha256 != digest:
            # Same filename, different bytes. The name is content-derived, so this
            # should be impossible — but if it happens the cached upload is stale.
            current.sha256 = digest
            current.telegram_file_id = None
            stats["figure_changed"] += 1
        else:
            stats["figure_unchanged"] += 1
    session.flush()

    # --- quesiti ---------------------------------------------------------------
    existing_quesiti = {q.id: q for q in session.scalars(select(Quesito))}
    for entry in data["quesiti"]:
        topic_name = data["topics"][entry["topic_id"] - 1]["name"]
        tid = topic_id[topic_name]
        current = existing_quesiti.get(entry["id"])
        if current is None:
            session.add(
                Quesito(id=entry["id"], topic_id=tid, primary_image=entry["primary_image"])
            )
            stats["quesito_new"] += 1
        elif (current.topic_id, current.primary_image) != (tid, entry["primary_image"]):
            current.topic_id = tid
            current.primary_image = entry["primary_image"]
            stats["quesito_changed"] += 1
        else:
            stats["quesito_unchanged"] += 1
    session.flush()

    # --- questions -------------------------------------------------------------
    existing = {q.id: q for q in session.scalars(select(Question))}
    incoming_ids = set()
    recluster: list[int] = []

    for q in data["questions"]:
        incoming_ids.add(q["id"])
        tid = topic_id[q["topic"]]
        current = existing.get(q["id"])
        if current is None:
            session.add(
                Question(
                    id=q["id"],
                    quesito_id=q["quesito_id"],
                    topic_id=tid,
                    stem_it=q["stem_it"],
                    statement_it=q["statement_it"],
                    answer=q["answer"],
                    image_path=q["image"],
                    source_version=q["source_version"],
                )
            )
            stats["question_new"] += 1
            continue

        substantive = (
            current.statement_it != q["statement_it"] or current.answer != q["answer"]
        )
        metadata_changed = (
            current.quesito_id != q["quesito_id"]
            or current.topic_id != tid
            or current.stem_it != q["stem_it"]
            or current.image_path != q["image"]
            or current.source_version != q["source_version"]
        )
        if substantive:
            current.statement_it = q["statement_it"]
            current.answer = q["answer"]
            # The rule this question tested may no longer be the rule it tests.
            if current.cluster_id is not None:
                current.cluster_id = None
                recluster.append(q["id"])
            stats["question_rewritten"] += 1
        if metadata_changed:
            current.quesito_id = q["quesito_id"]
            current.topic_id = tid
            current.stem_it = q["stem_it"]
            current.image_path = q["image"]
            current.source_version = q["source_version"]
            if not substantive:
                stats["question_metadata"] += 1
        if not substantive and not metadata_changed:
            stats["question_unchanged"] += 1

    retired = sorted(set(existing) - incoming_ids)
    stats["question_retired"] = len(retired)
    if retired and prune:
        session.execute(delete(Question).where(Question.id.in_(retired)))
        stats["question_pruned"] = len(retired)

    session.flush()
    return stats, retired, recluster


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", type=Path, default=QUESTIONS_JSON)
    ap.add_argument("--dry-run", action="store_true", help="show the diff, write nothing")
    ap.add_argument("--prune", action="store_true", help="delete questions no longer in the listato")
    args = ap.parse_args()

    if not args.json.exists():
        print("ERROR: run content/extract.py first", file=sys.stderr)
        return 2

    data = json.loads(args.json.read_text(encoding="utf-8"))
    print(f"{args.json.name}: {data['counts']} (source {data['source_version']})")

    factory = sync_session_factory()
    with factory() as session:
        stats, retired, recluster = seed(session, data, prune=args.prune)

        width = max(len(k) for k in stats) if stats else 0
        for key in sorted(stats):
            if stats[key]:
                print(f"  {key:<{width}}  {stats[key]}")

        if retired:
            print(f"\n  {len(retired)} questions are no longer in the listato: {retired[:10]}")
            if not args.prune:
                print("  left in place — users have progress against them. "
                      "Pass --prune to remove.")
        if recluster:
            print(f"\n  {len(recluster)} questions changed substantively and were "
                  f"detached from their cluster: {recluster[:10]}")
            print("  their explanations must be regenerated before those topics go live.")

        if args.dry_run:
            session.rollback()
            print("\ndry run — rolled back, nothing written")
        else:
            session.commit()
            print("\ncommitted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
