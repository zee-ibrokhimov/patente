"""
review_export.py — write the review sheet.

Build step 7 (plan §14.4). §14.5 is explicit that this must not become a review UI:
export to CSV, read it in Google Sheets, mark a column, import it back. The
spreadsheet already sorts, filters and hands off to a native-speaker reviewer or a
paid istruttore without giving anyone access to anything.

WHAT A REVIEWER ACTUALLY NEEDS ON THE ROW
-----------------------------------------
The statements. An explanation cannot be judged next to nothing — "one approved
explanation serves every reworded variant" is only true if the reviewer can see the
variants it is claiming to serve. So each row carries the cluster's statements with
their ministerial answers, which is also what makes a `key`/`model` disagreement
checkable rather than just alarming.

THE FINGERPRINT COLUMN IS NOT DECORATION
----------------------------------------
Export, regenerate, then import an approval, and you would approve text that no
longer exists in the database. `fingerprint` is the first 12 hex of the sha256 of
the exact draft that was exported; `review_import.py` refuses a row whose draft has
changed underneath it. Do not sort by it, do not edit it, do not delete the column.

Usage:
    python content/review_export.py --topic "Segnali di precedenza"
    python content/review_export.py --status flagged        # the ones that need eyes
    python content/review_export.py --topic "..." --out sheet.csv
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import sys
from pathlib import Path

from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.models import Cluster, Explanation, Question, Topic  # noqa: E402
from shared.config import CONTENT_OUT  # noqa: E402
from shared.constants import EXPLANATION_STATUSES, LANG_IT, STATUS_APPROVED  # noqa: E402
from shared.db import sync_session_factory  # noqa: E402

DEFAULT_OUT = CONTENT_OUT / "review.csv"

# Prints ministerial statements and explanation text to a cp1252 console. See the
# same guard in generate.py: an unmappable character must cost a glyph, not the run.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

# `decision`, `explanation_edited`, `reviewer` and `note` are the reviewer's
# columns. Everything else is context and must come back unchanged.
COLUMNS = [
    "natural_key", "cluster_id", "fingerprint",     # identity — do not edit
    "topic", "n_statements", "figure", "status", "flags",
    "statements",
    "explanation_it",
    "decision", "explanation_edited", "reviewer", "note",
]

INSTRUCTIONS = """\
  In the sheet:
    · filter `flags` for "argues against" first — those are the ones that can mean
      the ANSWER KEY is wrong, not the explanation. Check the statement against the
      PDF before deciding.
    · then filter `flags` for "number" — every numeric claim gets read against the
      article.
    · set `decision` to one of: approve, reject, edit
    · for `edit`, put the corrected Italian in `explanation_edited`; an explanation
      you rewrote has by definition been read, so it imports as approved.
    · put your name in `reviewer`. A topic goes live only when every explanation in
      it is approved (§3.3).
  Leave `decision` blank on anything you did not get to — import skips those."""


def fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def render_statements(members: list[tuple[int, str, bool]]) -> str:
    """One statement per line, answer first, so the cell reads as a block."""
    return "\n".join(
        f"{'VERO ' if answer else 'FALSO'}  [{qid}]  {statement}"
        for qid, statement, answer in sorted(members)
    )


def collect(session, lang: str, topic: str | None, statuses: list[str] | None) -> list[dict]:
    query = (
        select(Explanation, Cluster.natural_key, Topic.name)
        .join(Cluster, Cluster.id == Explanation.cluster_id)
        .join(Topic, Topic.id == Cluster.topic_id)
        .where(Explanation.lang == lang)
        .order_by(Topic.name, Explanation.cluster_id)
    )
    if topic:
        query = query.where(Topic.name.like(f"{topic}%"))
    if statuses:
        query = query.where(Explanation.status.in_(statuses))

    found = list(session.execute(query))
    if not found:
        return []

    members: dict[int, list[tuple[int, str, bool]]] = {}
    images: dict[int, str] = {}
    ids = [explanation.cluster_id for explanation, _key, _topic in found]
    for cid, qid, statement, answer, image in session.execute(
        select(Question.cluster_id, Question.id, Question.statement_it,
               Question.answer, Question.image_path)
        .where(Question.cluster_id.in_(ids))
    ):
        members.setdefault(cid, []).append((qid, statement, bool(answer)))
        if image and cid not in images:
            images[cid] = image

    rows = []
    for explanation, natural_key, topic_name in found:
        cluster_members = members.get(explanation.cluster_id, [])
        rows.append({
            "natural_key": natural_key,
            "cluster_id": explanation.cluster_id,
            "fingerprint": fingerprint(explanation.text),
            "topic": topic_name.split(";")[0],
            "n_statements": len(cluster_members),
            "figure": images.get(explanation.cluster_id, ""),
            "status": explanation.status,
            "flags": explanation.flags or "",
            "statements": render_statements(cluster_members),
            "explanation_it": explanation.text,
            "decision": "",
            "explanation_edited": "",
            "reviewer": "",
            "note": "",
        })
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--topic", help="ministerial topic name, or a prefix of one")
    ap.add_argument("--status", nargs="*", choices=EXPLANATION_STATUSES,
                    help="only these statuses (default: everything not yet approved)")
    ap.add_argument("--lang", default=LANG_IT)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--all", action="store_true",
                    help="include rows already approved, for a second opinion")
    args = ap.parse_args()

    statuses = args.status
    if not statuses and not args.all:
        # The default is the working set: what still needs a human.
        statuses = [s for s in EXPLANATION_STATUSES if s != STATUS_APPROVED]

    factory = sync_session_factory()
    with factory() as session:
        rows = collect(session, args.lang, args.topic, statuses)

    if not rows:
        print("nothing to review — no explanations matched. Has content/generate.py run?",
              file=sys.stderr)
        return 2

    args.out.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-sig: Excel and Google Sheets both read accented Italian wrongly without it.
    with args.out.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    by_status: dict[str, int] = {}
    flagged = 0
    for row in rows:
        by_status[row["status"]] = by_status.get(row["status"], 0) + 1
        if row["flags"]:
            flagged += 1
    topics = sorted({row["topic"] for row in rows})

    print(f"{len(rows)} explanations -> {args.out}")
    print(f"  topics : {', '.join(topics)}")
    print(f"  status : {', '.join(f'{n} {s}' for s, n in sorted(by_status.items()))}")
    print(f"  carrying at least one flag : {flagged}")
    print(f"\n{INSTRUCTIONS}")
    print(f"\n  then: python content/review_import.py --in {args.out.name} --dry-run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
