"""
fetch_norms.py — pull the Codice della Strada and its Regolamento from Normattiva.

Build step 6 groundwork (plan §3.3, step 1). Explanations are generated *from this
text*, which is what makes them yours: the ministerial questions are public
documents and the legal reasoning is derived from statute, not from an autoscuola
manual. Nothing here comes from a copyrighted commentary.

SOURCE
------
Normattiva is the authoritative consolidated text and its URN permalinks resolve
without JavaScript:

    https://www.normattiva.it/uri-res/N2Ls?urn:nir:stato:...;285~art142

Italian statutes are `atti ufficiali dello Stato` and fall outside copyright
(art. 5, L. 633/1941), so the text is free to reuse. Attribute the source anyway.

WHY THE REGOLAMENTO MATTERS MORE THAN IT LOOKS
----------------------------------------------
Sign questions are the largest and cheapest part of the bank to explain, and the
signs themselves are defined in the Regolamento, not the Codice. Better still,
every sign is named in capitals and cross-referenced to a figure:

    Art. 116 (Segnali di divieto generici)
    a) il segnale DIVIETO DI TRANSITO (fig. II.46);
    b) il segnale SENSO VIETATO (fig. II.47);

Those capitalised names are directly matchable against the ministerial statement
wording, which is what lets a cluster be tied to the article that governs it.

Usage:
    python content/fetch_norms.py --source both
    python content/fetch_norms.py --source reg --first 77 --last 136   # signs only
"""

from __future__ import annotations

import argparse
import html as htmllib
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.config import CONTENT_OUT  # noqa: E402

NORMS_DIR = CONTENT_OUT / "norms"
BASE = "https://www.normattiva.it/uri-res/N2Ls?"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

SOURCES = {
    "cds": {
        "key": "cds",
        "label": "D.Lgs. 30 aprile 1992, n. 285 — Nuovo codice della strada",
        "short": "CdS",
        "urn": "urn:nir:stato:decreto.legislativo:1992-04-30;285",
        "articles": 245,
    },
    "reg": {
        "key": "regolamento",
        "label": "D.P.R. 16 dicembre 1992, n. 495 — Regolamento di esecuzione",
        "short": "Reg.",
        "urn": "urn:nir:stato:decreto.del.presidente.della.repubblica:1992-12-16;495",
        "articles": 408,
    },
}

BODY_RE = re.compile(r'<div[^>]*class="[^"]*bodyTesto[^"]*"[^>]*>(.*?)</div>\s*</div>', re.S)
HEAD_RE = re.compile(r"Art\.?\s*(\d+(?:[-\s]?(?:bis|ter|quater|quinquies|sexies))?)\s*", re.I)
TAIL_RE = re.compile(r"articolo\s+precedente|articolo\s+successivo", re.I)
# The Regolamento cross-references every sign to a plate: "(fig. II.46)".
FIGURE_RE = re.compile(r"fig(?:ura)?\.?\s*([IVX]+\.\s*\d+[a-z]?)", re.I)
# Sign names are the only ALL-CAPS runs in the text, which is what makes them findable.
SIGNNAME_RE = re.compile(r"\b([A-ZÀÈÉÌÒÙ][A-ZÀÈÉÌÒÙ'\s]{4,60}[A-ZÀÈÉÌÒÙ])\b")


def strip_html(fragment: str) -> str:
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", fragment, flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>|</p>|</div>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = htmllib.unescape(text)
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def parse_article(raw_html: str) -> dict | None:
    """Pull one article's number, rubric and body out of a Normattiva page."""
    match = BODY_RE.search(raw_html)
    if not match:
        return None
    text = strip_html(match.group(1))

    head = HEAD_RE.search(text)
    if not head:
        return None
    number = re.sub(r"[\s-]+", "-", head.group(1).strip().lower())
    text = text[head.end():].lstrip()

    # The Codice writes the rubric bare, the Regolamento parenthesises it.
    rubric = ""
    if text.startswith("("):
        close = text.find(")")
        if close > 0:
            rubric, text = text[1:close].strip(), text[close + 1:].lstrip()
    else:
        first_comma = re.search(r"\s1\s*\.\s", text)
        if first_comma and first_comma.start() < 200:
            rubric, text = text[: first_comma.start()].strip(), text[first_comma.start():].lstrip()

    rubric = re.sub(r"\s+", " ", rubric).strip(" ,;")

    cut = TAIL_RE.search(text)
    if cut:
        text = text[: cut.start()].rstrip()

    text = re.sub(r"\s*\n\s*", " ", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    if not text or len(text) < 12:
        return None

    return {
        "number": number,
        "rubric": rubric,
        "text": text,
        "figures": sorted({re.sub(r"\s+", "", f) for f in FIGURE_RE.findall(text)}),
        "sign_names": sorted({
            re.sub(r"\s+", " ", n).strip()
            for n in SIGNNAME_RE.findall(text)
            if not n.strip().isspace()
        }),
    }


def fetch_source(spec: dict, first: int, last: int, delay: float, existing: dict) -> dict:
    articles: dict[str, dict] = dict(existing)
    misses = 0
    with httpx.Client(
        headers={"User-Agent": USER_AGENT}, timeout=60, follow_redirects=True
    ) as client:
        for n in range(first, last + 1):
            if str(n) in articles:
                continue
            url = f"{BASE}{spec['urn']}~art{n}"
            try:
                response = client.get(url)
                parsed = parse_article(response.text) if response.status_code == 200 else None
            except Exception as exc:  # noqa: BLE001
                print(f"    art {n}: {type(exc).__name__} {exc}")
                parsed = None

            if parsed is None:
                misses += 1
                print(f"    art {n}: no text")
            else:
                misses = 0
                parsed["url"] = url
                articles[parsed["number"]] = parsed
                flag = f" [{len(parsed['figures'])} fig]" if parsed["figures"] else ""
                print(f"    art {parsed['number']:>7}  {len(parsed['text']):>6} ch  "
                      f"{parsed['rubric'][:56]}{flag}")

            if misses >= 12:
                print(f"    stopping: {misses} consecutive articles with no text")
                break
            time.sleep(delay)
    return articles


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=("cds", "reg", "both"), default="both")
    ap.add_argument("--first", type=int, default=1)
    ap.add_argument("--last", type=int, default=None)
    ap.add_argument("--delay", type=float, default=0.4, help="seconds between requests")
    ap.add_argument("--refetch", action="store_true", help="ignore what is already saved")
    args = ap.parse_args()

    NORMS_DIR.mkdir(parents=True, exist_ok=True)
    chosen = ("cds", "reg") if args.source == "both" else (args.source,)

    for key in chosen:
        spec = SOURCES[key]
        path = NORMS_DIR / f"{spec['key']}.json"
        existing = {}
        if path.exists() and not args.refetch:
            existing = {a["number"]: a for a in json.loads(path.read_text("utf-8"))["articles"]}
            print(f"{spec['short']}: {len(existing)} articles already saved")

        last = args.last or spec["articles"]
        print(f"\n{spec['label']}\n  fetching articles {args.first}..{last}")
        articles = fetch_source(spec, args.first, last, args.delay, existing)

        ordered = sorted(
            articles.values(),
            key=lambda a: (int(re.match(r"\d+", a["number"]).group()), a["number"]),
        )
        path.write_text(
            json.dumps(
                {
                    "source": spec["label"],
                    "short": spec["short"],
                    "urn": spec["urn"],
                    "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "count": len(ordered),
                    "articles": ordered,
                },
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
        chars = sum(len(a["text"]) for a in ordered)
        figures = {f for a in ordered for f in a["figures"]}
        print(f"\n  {len(ordered)} articles, {chars/1000:.0f}k characters, "
              f"{len(figures)} distinct figure references")
        print(f"  -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
