"""
verify_sample.py — build the hand-verification sheet for the M1 gate (plan §14.7).

Produces content/out/verify.html: open it beside the source PDF and check each
row against the stated page. Every item shows its PDF page number, so checking is
a lookup, not a search.

Two samples, because they answer different questions:

  RANDOM 30    stratified across the 25 topics. This is the plan's gate and it
               estimates the overall error rate.

  HIGH RISK    rows drawn from the three places where a parsing bug would hide
               and a random sample would almost certainly miss:
                 · merged-answer rows — the 68 statements long enough that the
                   PDF text layer glues VERO/FALSO onto the statement line
                 · page-straddling rows — the first row on a page, which belongs
                   to the previous page's quesito
                 · composite-figure rows — "il segnale (A) ... il segnale (B)"
                   items whose figure differs from the rest of their group
               A clean random 30 with a broken high-risk set means the bank is
               poisoned in exactly the way that is invisible downstream.

Usage:
    python content/verify_sample.py            # 30 random + all high-risk classes
    python content/verify_sample.py --n 50 --seed 7
"""

from __future__ import annotations

import argparse
import collections
import html
import json
import random
import sys
from pathlib import Path

import fitz

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.config import CONTENT_OUT, QUESTIONS_JSON, SOURCE_PDF  # noqa: E402

X_TEXT, X_ANSWER, TOL = 77.5, 355.0, 1.5
OUT = CONTENT_OUT / "verify.html"


def merged_answer_ids(pdf: Path) -> set[int]:
    """Statement numbers whose answer merged into the statement line in the PDF."""
    doc = fitz.open(pdf)
    ids: set[int] = set()
    for pno in range(doc.page_count):
        rows = []  # (y, number)
        merged_y = []
        for block in doc[pno].get_text("dict")["blocks"]:
            if block["type"] != 0:
                continue
            for line in block["lines"]:
                x0, y0 = line["bbox"][0], line["bbox"][1]
                text = "".join(s["text"] for s in line["spans"]).strip()
                if abs(x0 - 22.0) < TOL and text.isdigit():
                    rows.append((y0, int(text)))
                elif abs(x0 - X_TEXT) < TOL and line["bbox"][2] > X_ANSWER:
                    if any(
                        abs(s["bbox"][0] - X_ANSWER) < TOL and s["text"].strip() in ("VERO", "FALSO")
                        for s in line["spans"]
                    ):
                        merged_y.append(y0)
        rows.sort()
        for y in merged_y:
            candidates = [n for (ry, n) in rows if abs(ry - y) < 2]
            ids.update(candidates)
    doc.close()
    return ids


def first_row_per_page(questions: list[dict]) -> set[int]:
    seen: dict[int, int] = {}
    for q in questions:
        seen.setdefault(q["page"], q["id"])
    return set(seen.values())


def composite_figure_ids(questions: list[dict]) -> set[int]:
    by_q: dict[int, list[dict]] = collections.defaultdict(list)
    for q in questions:
        by_q[q["quesito_id"]].append(q)
    out: set[int] = set()
    for group in by_q.values():
        images = collections.Counter(q["image"] for q in group if q["image"])
        if len(images) > 1:
            common = images.most_common(1)[0][0]
            out.update(q["id"] for q in group if q["image"] and q["image"] != common)
    return out


CSS = """
body{font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;margin:0;padding:24px;
     background:#fafafa;color:#1a1a1a;max-width:1100px;margin:0 auto}
h1{font-size:22px;margin:0 0 4px}
h2{font-size:17px;margin:32px 0 6px;padding-top:14px;border-top:2px solid #ddd}
.sub{color:#666;margin:0 0 18px;font-size:14px}
table{border-collapse:collapse;width:100%;background:#fff;
      box-shadow:0 1px 3px rgba(0,0,0,.08);border-radius:6px;overflow:hidden}
th{background:#f0f0f0;text-align:left;padding:9px 10px;font-size:13px;
   text-transform:uppercase;letter-spacing:.03em;color:#555}
td{padding:10px;border-top:1px solid #eee;vertical-align:top}
td.n{font-variant-numeric:tabular-nums;white-space:nowrap;color:#666;font-size:13px}
td.page{font-weight:600;font-variant-numeric:tabular-nums;white-space:nowrap}
img{width:74px;height:74px;object-fit:contain;background:#fff;border:1px solid #e2e2e2;
    border-radius:4px;display:block}
.ans{font-weight:700;white-space:nowrap;letter-spacing:.02em}
.v{color:#0a7d33}.f{color:#c0271c}
.topic{color:#777;font-size:12px;margin-top:5px}
.tick{width:26px;text-align:center;color:#bbb;font-size:19px}
.why{background:#fff8e1;border-left:3px solid #f0b429;padding:2px 7px;
     border-radius:3px;font-size:11.5px;color:#7a5600;display:inline-block;margin-top:5px}
@media print{body{background:#fff}table{box-shadow:none}}
"""


def render(rows: list[tuple[dict, str | None]], title: str, note: str) -> str:
    out = [f"<h2>{html.escape(title)}</h2><p class=sub>{html.escape(note)}</p>",
           "<table><tr><th class=tick>✓</th><th>PDF page</th><th>id</th>"
           "<th>statement</th><th>answer</th><th>figure</th></tr>"]
    for q, why in rows:
        cls = "v" if q["answer"] else "f"
        word = "VERO" if q["answer"] else "FALSO"
        img = f'<img src="{q["image"]}" alt="">' if q["image"] else "<span class=n>—</span>"
        why_html = f'<div class="why">{html.escape(why)}</div>' if why else ""
        out.append(
            f'<tr><td class=tick>☐</td><td class=page>p.{q["page"]}</td>'
            f'<td class=n>{q["id"]}</td>'
            f'<td>{html.escape(q["statement_it"])}'
            f'<div class=topic>{html.escape(q["topic"][:78])}</div>{why_html}</td>'
            f'<td class="ans {cls}">{word}</td><td>{img}</td></tr>'
        )
    out.append("</table>")
    return "".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=30, help="size of the random sample")
    ap.add_argument("--seed", type=int, default=20250423, help="reproducible sample")
    ap.add_argument("--risk-per-class", type=int, default=8)
    args = ap.parse_args()

    if not QUESTIONS_JSON.exists():
        print("ERROR: run content/extract.py first", file=sys.stderr)
        return 2

    data = json.loads(QUESTIONS_JSON.read_text(encoding="utf-8"))
    questions = data["questions"]
    by_id = {q["id"]: q for q in questions}
    rng = random.Random(args.seed)

    # Stratified random sample: spread across topics, remainder filled at random.
    by_topic: dict[str, list[dict]] = collections.defaultdict(list)
    for q in questions:
        by_topic[q["topic"]].append(q)
    picked: list[dict] = []
    for topic in sorted(by_topic):
        picked.append(rng.choice(by_topic[topic]))
    pool = [q for q in questions if q["id"] not in {p["id"] for p in picked}]
    picked += rng.sample(pool, max(0, args.n - len(picked)))
    picked = picked[: args.n]
    picked.sort(key=lambda q: q["page"])

    print("scanning the PDF for merged-answer rows ...")
    merged = merged_answer_ids(SOURCE_PDF)
    straddle = first_row_per_page(questions)
    composite = composite_figure_ids(questions)
    print(f"  merged-answer rows : {len(merged)}")
    print(f"  page-straddling    : {len(straddle)}")
    print(f"  composite-figure   : {len(composite)}")

    risk_rows: list[tuple[dict, str]] = []
    for ids, label in (
        (merged, "answer was glued onto the statement line in the PDF"),
        (straddle, "first row on its page — belongs to the previous page's quesito"),
        (composite, "figure differs from the rest of its quesito"),
    ):
        chosen = sorted(ids & set(by_id))
        for qid in rng.sample(chosen, min(args.risk_per_class, len(chosen))):
            risk_rows.append((by_id[qid], label))
    risk_rows.sort(key=lambda r: r[0]["page"])

    body = [
        f"<h1>Hand-verification sheet — {html.escape(data['source_file'])}</h1>",
        f"<p class=sub>{data['counts']['statements']} statements · "
        f"{data['counts']['quesiti']} quesiti · {data['counts']['topics']} topics · "
        f"{data['counts']['figures']} figures · seed {args.seed}. "
        "Open the PDF at the stated page and confirm the statement text, the "
        "VERO/FALSO answer and the figure all match.</p>",
        render([(q, None) for q in picked], f"Random sample ({len(picked)})",
               "Stratified across all 25 topics. This estimates the overall error rate — "
               "the plan's M1 gate."),
        render(risk_rows, f"High-risk sample ({len(risk_rows)})",
               "Rows drawn from the three classes where a parsing bug would hide. A random "
               "sample would almost never reach these."),
    ]
    OUT.write_text(f"<!doctype html><meta charset=utf-8><title>Verify</title>"
                   f"<style>{CSS}</style>{''.join(body)}", encoding="utf-8")
    print(f"\nOK -> {OUT}")
    print(f"   {len(picked)} random + {len(risk_rows)} high-risk rows to check by hand")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
