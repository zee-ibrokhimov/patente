"""
extract.py — official AB listato PDF -> content/out/questions.json + images/

Build step 1 (plan §14.4). This is the critical path: everything downstream
(translations, explanations, the paywall) is only as trustworthy as the
statement<->answer pairing produced here.

WHY THIS IS GEOMETRIC, NOT TEXT-FLOW
------------------------------------
Reading the PDF's text in flow order looks like it works and is quietly wrong.
Four defects in this source, all verified against the 2025-04-23 listato:

  A. On 68 rows the statement is long enough to reach the answer column, and the
     PDF text layer merges "VERO"/"FALSO" into the statement line. A parser that
     takes "the next VERO/FALSO in reading order" silently borrows the *following*
     row's answer and shifts the entire answer key by one from that point on.
     Fix: classify every word by its x coordinate. The answer always sits at
     x=355.0 whether or not it merged into the line.

  B. 142 figure placements sit outside the nominal x=465.6 image column (narrow
     portrait signs are centred, drifting to x=486.8). A tight x match drops
     their figures, leaving sign questions that are unanswerable as text.
     Fix: the image cell is any image block with x0 > 430, which also excludes
     the ministry logo on page 1 (x0=275.2).

  C. Long topic names wrap onto continuation lines that begin at x=20.0 but run
     to x=570 — straight through the statement and answer columns. Left in the
     pool they get absorbed into the preceding row's statement text. On three
     pages the wrap does not fit and its last line spills onto the next page
     (quesito 4422's title starts at y=790.6 on page 505 of 590).
     Fix: titles and their continuations are removed before word classification,
     the continuation run is bounded positionally (title -> table header), and a
     run reaching the page bottom continues into the next page's leading lines.

  D. Statement groups (quesiti) span page boundaries, and the section title names
     the quesito that *begins* on the page, not the one at the top of it. Page 2
     is titled 4329 while its first two rows still belong to 4328.
     Fix: walk titles and rows interleaved in (page, y) order, carrying the
     current quesito across page breaks.

Usage:
    python content/extract.py            # extract, validate, write
    python content/extract.py --report   # also print a per-topic breakdown
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import fitz  # PyMuPDF

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.config import (  # noqa: E402
    CONTENT_OUT,
    EXPECTED_QUESITI,
    EXPECTED_STATEMENTS,
    EXPECTED_TOPICS,
    IMAGES_DIR,
    QUESTIONS_JSON,
    SOURCE_PDF,
    SOURCE_VERSION,
)

# --- Column geometry, measured from the source ------------------------------
X_TITLE = 20.0   # "Quesito n° NNNN - <topic>" and its wrapped continuations
X_NUM = 22.0     # numero domanda
X_TEXT = 77.5    # testo domanda
X_ANSWER = 355.0 # risposta corretta
X_IMAGE_MIN = 430.0  # anything further right is the figure cell
TOL = 1.5        # the source is machine-generated; columns do not drift

TITLE_RE = re.compile(r"^Quesito\s+n°\s*(\d+)\s*-\s*(.*)$")
NUM_RE = re.compile(r"^\d{2,7}$")
ANSWER_RE = re.compile(r"^(VERO|FALSO)$")
TABLE_HEADER = {
    "Numero", "domanda", "Testo domanda", "Risposta Corretta", "Immagine",
    "Ministero delle Infrastrutture e dei Trasporti",
}


def norm(s: str) -> str:
    """Whitespace and apostrophe normalisation only.

    Ministerial wording is what users train to recognise, so nothing here may
    change words. U+00BF is a mojibake apostrophe in this source ("dell¿ambiente")
    and appears only inside topic names.
    """
    s = s.replace("’", "'").replace(" ", " ")
    s = re.sub(r"(?<=\w)¿(?=\w)", "'", s)
    return re.sub(r"\s+", " ", s).strip()


class Line:
    __slots__ = ("y", "x0", "x1", "text", "spans")

    def __init__(self, line: dict):
        self.y = line["bbox"][1]
        self.x0 = line["bbox"][0]
        self.x1 = line["bbox"][2]
        self.spans = line["spans"]
        self.text = "".join(s["text"] for s in self.spans).strip()

    def at(self, x: float) -> bool:
        return abs(self.x0 - x) < TOL


def read_page(page) -> tuple[list[Line], list[dict]]:
    text_lines: list[Line] = []
    image_blocks: list[dict] = []
    for block in page.get_text("dict")["blocks"]:
        if block["type"] == 1:
            if block["bbox"][0] > X_IMAGE_MIN:
                image_blocks.append(block)
            continue
        for raw in block["lines"]:
            line = Line(raw)
            if line.text:
                text_lines.append(line)
    text_lines.sort(key=lambda ln: (ln.y, ln.x0))
    image_blocks.sort(key=lambda b: b["bbox"][1])
    return text_lines, image_blocks


def leading_continuations(lines: list[Line]) -> list[Line]:
    """x=20.0 non-title lines at the very top of a page.

    These are the tail of a topic name that wrapped across the page break. They
    belong to the previous page's last title, never to anything on this page.
    """
    out: list[Line] = []
    for ln in lines:
        if ln.at(X_TITLE) and not TITLE_RE.match(ln.text):
            out.append(ln)
        else:
            break
    return out


def split_titles(
    lines: list[Line], spill: list[Line] | None = None
) -> tuple[list[tuple[float, int, str]], list[Line]]:
    """Pull out quesito titles (with their wrapped continuations).

    Returns (titles, remaining_lines). A continuation is any x=20.0 line between
    the title and the next structural boundary — the table header that always
    follows, the next title, or the page bottom. Bounding it positionally rather
    than by line adjacency is what makes this survive topic names that wrap to
    three lines. `spill` carries the next page's leading continuation lines, used
    only when the run reaches the bottom of this page.
    """
    titles: list[tuple[float, int, str]] = []
    consumed: set[int] = set()

    title_idx = [(i, TITLE_RE.match(ln.text)) for i, ln in enumerate(lines)]
    title_idx = [(i, m) for i, m in title_idx if m and lines[i].at(X_TITLE)]

    for pos, (i, m) in enumerate(title_idx):
        consumed.add(i)
        title_y = lines[i].y
        # Boundary: next title, or the first table-header/row line below the title.
        boundary = float("inf")
        if pos + 1 < len(title_idx):
            boundary = min(boundary, lines[title_idx[pos + 1][0]].y)
        for ln in lines:
            if ln.y > title_y and not ln.at(X_TITLE):
                boundary = min(boundary, ln.y)
                break
        parts = [m.group(2)]
        for j, ln in enumerate(lines):
            if j in consumed or not ln.at(X_TITLE):
                continue
            if title_y < ln.y < boundary and not TITLE_RE.match(ln.text):
                parts.append(ln.text)
                consumed.add(j)
        # Nothing structural followed this title, so its topic name ran off the
        # bottom of the page — pick up the remainder from the next page.
        if boundary == float("inf") and pos == len(title_idx) - 1 and spill:
            parts.extend(ln.text for ln in spill)
        titles.append((title_y, int(m.group(1)), norm(" ".join(parts))))

    remaining = [ln for i, ln in enumerate(lines) if i not in consumed]
    return titles, remaining


def extract_rows(lines: list[Line], images: list[dict]) -> list[dict]:
    """Build statement rows, anchored on the numero cell and bounded by the next one."""
    body = [ln for ln in lines if ln.text not in TABLE_HEADER]
    anchors = [ln for ln in body if ln.at(X_NUM) and NUM_RE.match(ln.text)]

    rows = []
    for i, anchor in enumerate(anchors):
        top = anchor.y - 2.0
        bottom = anchors[i + 1].y - 2.0 if i + 1 < len(anchors) else float("inf")

        text_parts: list[str] = []
        answer: str | None = None
        for ln in body:
            if not (top <= ln.y < bottom):
                continue
            if ln.at(X_TEXT):
                # Defect A: the answer may have merged into this line. Spans keep
                # their own x, so split on the span sitting in the answer column.
                for span in ln.spans:
                    txt = span["text"].strip()
                    if not txt:
                        continue
                    if abs(span["bbox"][0] - X_ANSWER) < TOL and ANSWER_RE.match(txt):
                        answer = txt
                    else:
                        text_parts.append(txt)
            elif ln.at(X_ANSWER) and ANSWER_RE.match(ln.text):
                answer = ln.text

        image = next(
            (b for b in images if top <= b["bbox"][1] < bottom), None
        )
        rows.append(
            {
                "number": int(anchor.text),
                "statement_it": norm(" ".join(text_parts)),
                "answer": answer,
                "image_block": image,
                "y": anchor.y,
            }
        )
    return rows


def raw_census(doc) -> tuple[collections.Counter, set[int]]:
    """Count answers and statement numbers straight off word coordinates.

    Deliberately shares no code with the row builder. If this disagrees with the
    parsed output, the statement<->answer pairing has drifted somewhere — which is
    the one failure mode that is invisible in the finished product.
    """
    answers: collections.Counter = collections.Counter()
    numbers: set[int] = set()
    for pno in range(doc.page_count):
        for w in doc[pno].get_text("words"):
            x0, word = w[0], w[4]
            if abs(x0 - X_ANSWER) < TOL and ANSWER_RE.match(word):
                answers[word] += 1
            elif abs(x0 - X_NUM) < TOL and NUM_RE.match(word):
                numbers.add(int(word))
    return answers, numbers


def extract(pdf_path: Path) -> dict:
    doc = fitz.open(pdf_path)
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)

    images_by_hash: dict[str, str] = {}
    quesiti: dict[int, dict] = {}
    questions: list[dict] = []
    current_qid: int | None = None
    problems: list[str] = []

    cache: dict[int, tuple[list[Line], list[dict]]] = {}

    def page_data(i: int):
        if i not in cache:
            cache[i] = read_page(doc[i])
        return cache[i]

    for pno in range(doc.page_count):
        lines, image_blocks = page_data(pno)
        spill = (
            leading_continuations(page_data(pno + 1)[0]) if pno + 1 < doc.page_count else None
        )
        cache.pop(pno - 1, None)
        titles, body = split_titles(lines, spill)
        rows = extract_rows(body, image_blocks)

        # Defect D: interleave titles and rows by y, carrying the quesito across
        # the page break. A row above the page's first title belongs to the
        # previous page's quesito.
        events = [("title", y, t) for (y, _q, t) in titles]
        events += [("row", r["y"], r) for r in rows]
        events.sort(key=lambda e: e[1])
        title_by_y = {y: (q, t) for (y, q, t) in titles}

        for kind, y, payload in events:
            if kind == "title":
                qid, topic = title_by_y[y]
                current_qid = qid
                if qid in quesiti and quesiti[qid]["topic"] != topic:
                    problems.append(f"quesito {qid} seen with two topics")
                quesiti.setdefault(
                    qid,
                    {"id": qid, "topic": topic, "page": pno, "figures": [], "statements": []},
                )
                continue

            row = payload
            if current_qid is None:
                problems.append(f"statement {row['number']} on p{pno} has no quesito")
                continue
            if row["answer"] is None:
                problems.append(f"statement {row['number']} on p{pno} has no answer")
                continue
            if not row["statement_it"]:
                problems.append(f"statement {row['number']} on p{pno} has no text")
                continue

            image_rel = None
            block = row["image_block"]
            if block is not None:
                data = block["image"]
                digest = hashlib.sha256(data).hexdigest()[:16]
                if digest not in images_by_hash:
                    ext = block.get("ext", "png")
                    name = f"{digest}.{ext}"
                    (IMAGES_DIR / name).write_bytes(data)
                    images_by_hash[digest] = name
                image_rel = f"images/{images_by_hash[digest]}"

            q = quesiti[current_qid]
            q["figures"].append(image_rel)
            q["statements"].append(row["number"])
            questions.append(
                {
                    "id": row["number"],
                    "quesito_id": current_qid,
                    "topic": q["topic"],
                    "stem_it": None,  # this listato has no textual stem; the figure is the stem
                    "statement_it": row["statement_it"],
                    "answer": row["answer"] == "VERO",
                    "image": image_rel,
                    "page": pno + 1,
                    "source_version": SOURCE_VERSION,
                }
            )

    raw_answers, raw_numbers = raw_census(doc)
    doc.close()
    return {
        "questions": questions,
        "quesiti": quesiti,
        "images": images_by_hash,
        "problems": problems,
        "raw_answers": raw_answers,
        "raw_numbers": raw_numbers,
    }


def validate(result: dict) -> list[str]:
    """Hard gate. Nothing is written if any of these fail (plan §14.7)."""
    questions = result["questions"]
    quesiti = result["quesiti"]
    errors = list(result["problems"])

    if len(questions) != EXPECTED_STATEMENTS:
        errors.append(f"statement count {len(questions)} != expected {EXPECTED_STATEMENTS}")
    if len(quesiti) != EXPECTED_QUESITI:
        errors.append(f"quesito count {len(quesiti)} != expected {EXPECTED_QUESITI}")

    topics = {q["topic"] for q in questions}
    if len(topics) != EXPECTED_TOPICS:
        errors.append(f"topic count {len(topics)} != expected {EXPECTED_TOPICS}")
        for t in sorted(topics):
            errors.append(f"    topic: {t[:110]}")

    ids = [q["id"] for q in questions]
    dupes = [i for i, c in collections.Counter(ids).items() if c > 1]
    if dupes:
        errors.append(f"{len(dupes)} duplicate statement ids: {dupes[:10]}")

    if any(q["answer"] not in (True, False) for q in questions):
        errors.append("non-boolean answer present")
    if any(not q["statement_it"] for q in questions):
        errors.append("empty statement text present")

    empty = [q for q in quesiti.values() if not q["statements"]]
    if empty:
        errors.append(f"{len(empty)} quesiti with no statements")

    # --- The alignment gate ---------------------------------------------------
    # A one-row slip between statement and answer key is silent: every question
    # still looks well-formed, the totals still add up, and the bank is wrong from
    # that row on. These two checks recount the source by word coordinate alone,
    # sharing no logic with the row builder, so a slip cannot satisfy both.
    out_answers = collections.Counter("VERO" if q["answer"] else "FALSO" for q in questions)
    if out_answers != result["raw_answers"]:
        errors.append(
            f"answer census mismatch — source {dict(result['raw_answers'])} "
            f"vs extracted {dict(out_answers)}"
        )

    out_numbers = set(ids)
    dropped = result["raw_numbers"] - out_numbers
    invented = out_numbers - result["raw_numbers"]
    if dropped:
        errors.append(f"{len(dropped)} statement numbers in the PDF are missing "
                      f"from the output: {sorted(dropped)[:10]}")
    if invented:
        errors.append(f"{len(invented)} statement numbers in the output are not in "
                      f"the PDF: {sorted(invented)[:10]}")

    # Sign-recognition questions are meaningless without their figure, and they
    # are a large share of the bank. Any statement that points at one must have it.
    figure_ref = re.compile(
        r"raffigurat|in figura|di figura|figura rappresenta|segnale \(|pannell[oi] \(", re.I
    )
    orphan = [q for q in questions if figure_ref.search(q["statement_it"]) and not q["image"]]
    if orphan:
        errors.append(f"{len(orphan)} statements reference a figure but have none: "
                      f"{[q['id'] for q in orphan][:10]}")

    return errors


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", type=Path, default=SOURCE_PDF)
    ap.add_argument("--report", action="store_true", help="print per-topic breakdown")
    args = ap.parse_args()

    if not args.pdf.exists():
        print(f"ERROR: source PDF not found: {args.pdf}", file=sys.stderr)
        return 2

    print(f"reading {args.pdf.name} ...")
    result = extract(args.pdf)
    errors = validate(result)

    questions, quesiti = result["questions"], result["quesiti"]
    print(f"  statements : {len(questions)}")
    print(f"  quesiti    : {len(quesiti)}")
    print(f"  topics     : {len({q['topic'] for q in questions})}")
    print(f"  figures    : {len(result['images'])} unique "
          f"({sum(1 for q in questions if q['image'])} statements carry one)")

    if errors:
        print("\nVALIDATION FAILED — nothing written:", file=sys.stderr)
        for e in errors[:40]:
            print(f"  - {e}", file=sys.stderr)
        if len(errors) > 40:
            print(f"  ... and {len(errors) - 40} more", file=sys.stderr)
        return 1

    topic_ids = {t: i + 1 for i, t in enumerate(sorted({q["topic"] for q in questions}))}
    for q in questions:
        q["topic_id"] = topic_ids[q["topic"]]

    CONTENT_OUT.mkdir(parents=True, exist_ok=True)
    payload = {
        "source_file": args.pdf.name,
        "source_version": SOURCE_VERSION,
        "extracted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "counts": {
            "statements": len(questions),
            "quesiti": len(quesiti),
            "topics": len(topic_ids),
            "figures": len(result["images"]),
        },
        "topics": [{"id": i, "name": t} for t, i in sorted(topic_ids.items(), key=lambda kv: kv[1])],
        "quesiti": [
            {
                "id": q["id"],
                "topic_id": topic_ids[q["topic"]],
                # The figure most of the group's statements share. Individual
                # statements may carry their own — "il segnale (A) ... il segnale (B)"
                # comparison items ship a composite image — so the authoritative
                # figure is always the one on the question row, not this one.
                "primary_image": (
                    collections.Counter(f for f in q["figures"] if f).most_common(1)[0][0]
                    if any(q["figures"])
                    else None
                ),
                "statements": sorted(q["statements"]),
            }
            for q in sorted(quesiti.values(), key=lambda x: x["id"])
        ],
        "questions": questions,
    }
    QUESTIONS_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nOK -> {QUESTIONS_JSON}")
    print(f"OK -> {IMAGES_DIR} ({len(result['images'])} files)")

    if args.report:
        print("\nper-topic breakdown:")
        counts = collections.Counter(q["topic"] for q in questions)
        with_img = collections.Counter(q["topic"] for q in questions if q["image"])
        for topic, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"  {n:5d}  ({with_img[topic]:4d} w/ figure)  {topic[:95]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
