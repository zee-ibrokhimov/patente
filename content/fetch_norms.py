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

BODY_OPEN_RE = re.compile(r'<div[^>]*class="[^"]*bodyTesto[^"]*"[^>]*>', re.I)
DIV_RE = re.compile(r"<(/?)div\b[^>]*>", re.I)
HEAD_RE = re.compile(r"Art\.?\s*(\d+(?:[-\s]?(?:bis|ter|quater|quinquies|sexies))?)\s*", re.I)
TAIL_RE = re.compile(r"articolo\s+precedente|articolo\s+successivo", re.I)
# Normattiva appends the article's full amendment history — "AGGIORNAMENTO (19) /
# Il Decreto 20 dicembre 1996 … ha disposto che le presenti modifiche avranno
# effetto dal …" — after the text, fenced off by a run of dashes. It is
# legislative provenance, not law, and feeding it to a generator is paying tokens
# to describe when a comma changed instead of what it says.
HISTORY_RE = re.compile(r"\n?-{3,}\s*\n?\s*AGGIORNAMENTO\s*\(")
# Footnote markers trailing each comma: "… sospensione della patente. (114) (124)".
FOOTNOTE_RE = re.compile(r"(?<=\s)\(\d{1,3}\)(?=\s|$)")
# The Regolamento cross-references every sign to a plate: "(fig. II.46)".
FIGURE_RE = re.compile(r"fig(?:ura)?\.?\s*([IVX]+\.\s*\d+[a-z]?)", re.I)
# A sign name is a run of capitals *bound to a plate* — "il segnale DIVIETO DI
# TRANSITO (fig. II.46)". Taking any capitalised run instead gives 251 "names"
# including STRADA, MOTORE and TONNELLATE, and matching statements against that
# list is worse than not matching at all. Requiring the plate gives 144, all real.
SIGN_RE = re.compile(
    r"([A-ZÀÈÉÌÒÙ][A-ZÀÈÉÌÒÙ'’\-\s]{3,70}?)\s*\(\s*fig(?:ura)?\.?\s*([IVX]+\.\s*\d+[a-z]?)"
)
# Leading connectives the capture drags in from "… e SENSO VIETATO (fig. II.47)".
SIGN_LEAD_RE = re.compile(r"^(?:E|O|A|DI|DEL|DELLA|IL|LA|LO|IN|CON|SU|PER|N|ART)\s+", re.I)


def body_fragment(raw_html: str) -> str | None:
    """The whole `bodyTesto` block, matched by counting div depth.

    A non-greedy regex up to the first `</div></div>` is the obvious way to do
    this and it is wrong, because Normattiva wraps every amended passage in its
    own div. The first nested close therefore ends the capture, and since almost
    every article of the Codice has been amended since 1992 the capture ends
    somewhere in the first comma. It is silent: the text that comes back is
    genuine article text, just truncated. Art. 148 lost commi 3-16 — the actual
    rules on overtaking — and art. 142 lost the speed-camera and sanction commi,
    which is precisely the material an explanation about speed limits needs.
    """
    open_tag = BODY_OPEN_RE.search(raw_html)
    if not open_tag:
        return None
    depth, start = 1, open_tag.end()
    for tag in DIV_RE.finditer(raw_html, start):
        depth += -1 if tag.group(1) else 1
        if depth == 0:
            return raw_html[start:tag.start()]
    return raw_html[start:]


def strip_html(fragment: str) -> str:
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", fragment, flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>|</p>|</div>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = htmllib.unescape(text)
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def parse_article(raw_html: str, expected: int | None = None) -> dict | None:
    """Pull one article's number, rubric and body out of a Normattiva page.

    `expected` is the article number that was actually requested, and passing it
    is not optional in practice. Normattiva does not 404 an article that does not
    resolve — abrogated articles, and anything past the end of the statute, come
    back as the decree's *preamble* page. The first "Art. N" in that page belongs
    to a law being cited in the recitals ("art. 4, comma 2, della legge 13 giugno
    1991, n. 190"), so a trusting parser reads the preamble as article 4 and
    files it under that key. Every unresolvable article in the range then lands
    on the same key and the last one wins — silently overwriting the real
    article 4. Refusing any page whose number is not the one requested turns that
    class of corruption back into a visible miss.
    """
    fragment = body_fragment(raw_html)
    if fragment is None:
        return None
    text = strip_html(fragment)

    history = HISTORY_RE.search(text)
    if history:
        text = text[: history.start()].rstrip(" -\n")

    # Normattiva marks every passage amended since 1992 with doubled parentheses.
    # The delimiters go, the text inside stays — including the "((...))" that marks
    # a deleted passage, which is genuine information about the article. Dropping
    # them before the rubric is read also stops "((Guida dopo l'assunzione…))"
    # being mistaken for a Regolamento-style parenthesised rubric and split at the
    # wrong bracket.
    text = text.replace("((", "").replace("))", "")
    text = FOOTNOTE_RE.sub("", text)

    head = HEAD_RE.search(text)
    if not head:
        return None
    number = re.sub(r"[\s-]+", "-", head.group(1).strip().lower())
    if expected is not None and int(re.match(r"\d+", number).group()) != expected:
        return None
    text = text[head.end():].lstrip()

    # The Codice writes the rubric bare, the Regolamento parenthesises it.
    rubric = ""
    if text.startswith("("):
        close = text.find(")")
        if close > 0:
            rubric, text = text[1:close].strip(), text[close + 1:].lstrip()
    else:
        # Normattiva's consolidated text wraps every amended passage in doubled
        # parentheses, so an article rewritten since 1992 opens "((1." rather than
        # "1." — art. 191 (pedoni) and art. 216 among them. Requiring a bare comma
        # number left those with no rubric at all, which matters because the rubric
        # is what a generated explanation cites the article *by*.
        first_comma = re.search(r"\s\(*\s*1\s*\.\s", text)
        if first_comma and first_comma.start() < 260:
            rubric, text = text[: first_comma.start()].strip(), text[first_comma.start():].lstrip()

    rubric = re.sub(r"\s+", " ", rubric).strip(" ,;")

    cut = TAIL_RE.search(text)
    if cut:
        text = text[: cut.start()].rstrip()

    text = re.sub(r"\s*\n\s*", " ", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    if not text or len(text) < 12:
        return None

    return {"number": number, "rubric": rubric, "text": text, **derive(text)}


def derive(text: str) -> dict:
    """The fields computed from the article text rather than fetched.

    Kept separate so `--reparse` can improve them without asking Normattiva for
    650 pages again. The text is the expensive part; this is arithmetic.
    """
    signs: dict[str, str] = {}
    for name, plate in SIGN_RE.findall(text):
        name = SIGN_LEAD_RE.sub("", re.sub(r"\s+", " ", name).strip(" -'’")).strip()
        if len(name) >= 5:
            signs.setdefault(name, re.sub(r"\s+", "", plate))
    return {
        "figures": sorted({re.sub(r"\s+", "", f) for f in FIGURE_RE.findall(text)}),
        "sign_names": sorted(signs),
        # name -> the plate it is defined against, which is what ties a sign
        # cluster to the article that governs it.
        "signs": dict(sorted(signs.items())),
    }


def fetch_source(
    spec: dict, first: int, last: int, delay: float, existing: dict, stop_after: int
) -> tuple[dict, list[int]]:
    articles: dict[str, dict] = dict(existing)
    absent: list[int] = []
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
                parsed = (
                    parse_article(response.text, expected=n)
                    if response.status_code == 200
                    else None
                )
            except Exception as exc:  # noqa: BLE001
                print(f"    art {n}: {type(exc).__name__} {exc}")
                parsed = None

            if parsed is None:
                misses += 1
                absent.append(n)
                print(f"    art {n}: no text")
            else:
                misses = 0
                parsed["url"] = url
                articles[parsed["number"]] = parsed
                flag = f" [{len(parsed['figures'])} fig]" if parsed["figures"] else ""
                print(f"    art {parsed['number']:>7}  {len(parsed['text']):>6} ch  "
                      f"{parsed['rubric'][:56]}{flag}")

            if misses >= stop_after:
                print(f"    stopping: {misses} consecutive articles with no text")
                break
            time.sleep(delay)
    return articles, absent


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=("cds", "reg", "both"), default="both")
    ap.add_argument("--first", type=int, default=1)
    ap.add_argument("--last", type=int, default=None)
    ap.add_argument("--delay", type=float, default=0.4, help="seconds between requests")
    ap.add_argument("--refetch", action="store_true", help="ignore what is already saved")
    ap.add_argument("--reparse", action="store_true",
                    help="recompute figures and sign names from the saved text, fetch nothing")
    # Abrogated articles are legitimate gaps, not the end of the statute, so this
    # has to tolerate a run of them before deciding it has walked off the end.
    ap.add_argument("--stop-after", type=int, default=20,
                    help="give up after this many consecutive articles with no text")
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

        if args.reparse:
            if not existing:
                print(f"  nothing saved for {spec['short']} — fetch it first", file=sys.stderr)
                continue
            before = sum(len(a.get("sign_names", ())) for a in existing.values())
            for article in existing.values():
                article.update(derive(article["text"]))
            after = sum(len(a["sign_names"]) for a in existing.values())
            print(f"\n{spec['label']}\n  reparsed {len(existing)} saved articles: "
                  f"{before} sign names -> {after}")
            articles, absent = existing, []
        else:
            last = args.last or spec["articles"]
            print(f"\n{spec['label']}\n  fetching articles {args.first}..{last}")
            articles, absent = fetch_source(
                spec, args.first, last, args.delay, existing, args.stop_after
            )

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
        # Printed rather than merely counted: most of these are articles the
        # legislator repealed, but a parser regression looks exactly the same from
        # here, and the difference is only visible if the numbers are on screen.
        if absent:
            print(f"  {len(absent)} requested articles returned no text "
                  f"(abrogated, or not in this statute): {absent}")
        print(f"  -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
