"""
review_import.py — read the reviewed sheet back and apply the decisions.

Build step 7, the other half. This is the only thing in the system that ever sets
`approved`, and `approved` is the only status the API will serve. Nothing generated
reaches a user without passing through here.

WHAT IT REFUSES TO DO
---------------------
  · Approve a draft that has changed since it was exported. `fingerprint` is checked
    against the text currently in the database; a mismatch means someone re-ran
    generate.py after exporting, and the reviewer read a different sentence from the
    one they would be approving. Skipped, listed, and `--force` is deliberately
    ugly to type.
  · Approve an empty explanation, or accept `edit` with nothing in
    `explanation_edited`.
  · Guess at a decision it does not recognise. "ok", "yes", "sì" are not `approve`.
    A typo in that column silently approving a row is exactly the failure this file
    exists to prevent.

THE RELEASE GATE
----------------
§3.3: a topic goes live only when 100% of its explanations have been read by a
human. That is a fact about the database, not a promise, so the run ends by printing
the approved fraction per topic and naming any topic that has just become complete.

Usage:
    python content/review_import.py --in content/out/review.csv --dry-run
    python content/review_import.py --in content/out/review.csv
"""

from __future__ import annotations

import argparse
import collections
import csv
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.models import Cluster, Explanation, Topic  # noqa: E402
from review_export import DEFAULT_OUT, fingerprint  # noqa: E402
from shared.constants import (  # noqa: E402
    LANG_IT,
    STATUS_APPROVED,
    STATUS_REJECTED,
)
from shared.db import sync_session_factory  # noqa: E402

DECISIONS = ("approve", "reject", "edit")

# Prints topic names and reviewer notes to a cp1252 console. See generate.py.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")


def normalise(value: str | None) -> str:
    return (value or "").strip().lower()


def apply_row(row: dict, explanation: Explanation, reviewer: str, now: datetime) -> str:
    """Returns the outcome word for the report, or raises ValueError to reject the row."""
    decision = normalise(row.get("decision"))
    edited = (row.get("explanation_edited") or "").strip()

    if decision == "approve":
        if edited and edited != explanation.text:
            raise ValueError(
                "decision is 'approve' but explanation_edited was filled in — use "
                "'edit' if the text should change, or clear the column"
            )
        explanation.status = STATUS_APPROVED
    elif decision == "edit":
        if not edited:
            raise ValueError("decision is 'edit' but explanation_edited is empty")
        # Text somebody rewrote has by definition been read.
        explanation.text = edited
        explanation.status = STATUS_APPROVED
    elif decision == "reject":
        explanation.status = STATUS_REJECTED
    else:
        raise ValueError(f"unrecognised decision {row.get('decision')!r} — "
                         f"must be one of {', '.join(DECISIONS)}")

    explanation.reviewed_at = now
    explanation.reviewer = reviewer
    # The flags described a draft that a human has now ruled on.
    explanation.flags = None
    return decision


def release_report(session, lang: str) -> None:
    """Approved fraction per topic — §3.3's gate, read off the database."""
    rows = session.execute(
        select(Topic.name, Explanation.status, func.count())
        .join(Cluster, Cluster.topic_id == Topic.id)
        .join(Explanation, Explanation.cluster_id == Cluster.id)
        .where(Explanation.lang == lang)
        .group_by(Topic.name, Explanation.status)
    ).all()
    if not rows:
        return

    per_topic: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for name, status, count in rows:
        per_topic[name.split(";")[0]][status] += count

    # A topic is only shippable when every cluster in it has an APPROVED
    # explanation — including the ones nobody has generated yet, which is why this
    # counts clusters rather than explanation rows.
    cluster_totals = dict(session.execute(
        select(Topic.name, func.count(Cluster.id))
        .join(Cluster, Cluster.topic_id == Topic.id)
        .group_by(Topic.name)
    ).all())

    print("\n  release status (§3.3: a topic ships at 100%)")
    complete = []
    for short in sorted(per_topic):
        counts = per_topic[short]
        total = next((n for name, n in cluster_totals.items()
                      if name.split(";")[0] == short), sum(counts.values()))
        approved = counts[STATUS_APPROVED]
        print(f"    {approved:>5}/{total:<5} {approved / total:>4.0%} approved  "
              f"{short[:52]}"
              + (f"   ({counts[STATUS_REJECTED]} rejected)" if counts[STATUS_REJECTED] else ""))
        if total and approved == total:
            complete.append(short)
    if complete:
        print(f"\n  READY TO RELEASE (100% approved): {', '.join(complete)}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="path", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--lang", default=LANG_IT)
    ap.add_argument("--reviewer", default="",
                    help="falls back to the sheet's reviewer column, then to $USER")
    ap.add_argument("--dry-run", action="store_true", help="report, write nothing")
    ap.add_argument("--force-stale-fingerprints", action="store_true",
                    help="apply decisions to drafts that changed after export")
    args = ap.parse_args()

    if not args.path.exists():
        print(f"ERROR: {args.path} not found — run content/review_export.py first",
              file=sys.stderr)
        return 2

    with args.path.open(newline="", encoding="utf-8-sig") as handle:
        sheet = list(csv.DictReader(handle))
    if not sheet:
        print("ERROR: the sheet is empty", file=sys.stderr)
        return 2
    missing = {"natural_key", "fingerprint", "decision"} - set(sheet[0])
    if missing:
        print(f"ERROR: the sheet is missing required columns: {sorted(missing)}. "
              f"Do not delete columns — re-export and paste the decisions across.",
              file=sys.stderr)
        return 2

    now = datetime.now(timezone.utc)
    stats: collections.Counter = collections.Counter()
    problems: list[str] = []

    factory = sync_session_factory()
    with factory() as session:
        by_key = {
            key: explanation
            for explanation, key in session.execute(
                select(Explanation, Cluster.natural_key)
                .join(Cluster, Cluster.id == Explanation.cluster_id)
                .where(Explanation.lang == args.lang)
            )
        }

        for line, row in enumerate(sheet, 2):     # 1 is the header, as the sheet shows it
            if not normalise(row.get("decision")):
                stats["left blank"] += 1
                continue

            explanation = by_key.get((row.get("natural_key") or "").strip())
            if explanation is None:
                problems.append(f"row {line}: no explanation for natural_key "
                                f"{row.get('natural_key')!r} in {args.lang}")
                stats["unmatched"] += 1
                continue

            if fingerprint(explanation.text) != (row.get("fingerprint") or "").strip():
                if not args.force_stale_fingerprints:
                    problems.append(
                        f"row {line}: the draft changed after this sheet was exported "
                        f"(cluster {explanation.cluster_id}) — you would be approving "
                        f"text you did not read"
                    )
                    stats["stale"] += 1
                    continue
                stats["stale, applied anyway"] += 1

            # Who approved a given explanation is the audit trail behind "reviewed by
            # a licensed instructor", so it must never quietly become a placeholder
            # when the sheet came back from someone else.
            reviewer = (args.reviewer or (row.get("reviewer") or "").strip()
                        or os.environ.get("USERNAME") or os.environ.get("USER")
                        or "unknown")
            try:
                stats[apply_row(row, explanation, reviewer, now)] += 1
            except ValueError as exc:
                problems.append(f"row {line}: {exc}")
                stats["rejected by validation"] += 1

        for key in sorted(stats):
            print(f"  {key:<26} {stats[key]}")
        if problems:
            print(f"\n  {len(problems)} rows not applied:")
            for problem in problems[:25]:
                print(f"    {problem}")
            if len(problems) > 25:
                print(f"    … and {len(problems) - 25} more")

        applied = sum(stats[decision] for decision in DECISIONS)
        if args.dry_run:
            session.rollback()
            print(f"\ndry run — {applied} decisions would apply, nothing written")
        elif applied:
            session.commit()
            print(f"\ncommitted {applied} decisions")
            release_report(session, args.lang)
        else:
            # Saying "committed" over an empty transaction is how a reviewer comes
            # away believing a sheet landed when every row was refused.
            session.rollback()
            print("\nno decisions applied — nothing written")

    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
