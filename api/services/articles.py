"""
articles.py — which articles of the law govern which ministerial topic.

Plan §3.3 step 2, and the input to explanation generation. Explanations are only
defensibly ours because they are derived from statute rather than from an autoscuola
manual, which means every generated explanation needs the *right* statute in front of
it. This module decides what that is.

It lives under `api/services/` rather than in `content/` because explanations are
generated on request now, not in an offline batch — so this is runtime business logic,
and `api/` owns all of that. `content/` still calls it, which is the direction
dependencies already run (`content/seed.py` imports `api.models`).

WHY THIS IS A HAND-WRITTEN TABLE AND NOT A MATCHER
--------------------------------------------------
`fetch_norms.py` extracts, for every Regolamento article, the sign names it defines
and the figure plates it binds them to — "il segnale DIVIETO DI TRANSITO
(fig. II.46)". That looked like it would make the mapping mechanical. Measured, it
does not, for one reason:

    the ministerial statements identify a sign by its picture, not by its name.

"Il segnale raffigurato indica un divieto di transito" contains none of the words
the Regolamento indexes on. Statement by statement, coverage is 25-40% on the sign
topics and under 5% on the text topics.

So the table below is the floor: 25 topics, hand-mapped, always available. On top of
it `signs_in` recovers likely-relevant articles for a cluster where some statement
mentions a sign by name, and uses them to *order* the article context.

**Ordering is all it is fit for.** A mention is not an identification: cluster 624 is
a stop sign whose statements say it stands at an intersection with a road holding the
DIRITTO DI PRECEDENZA, and cluster 623's statements mention the PREAVVISO sign that
precedes it. Substring matching cannot separate "is" from "faces" or "is preceded by".
An earlier version of this module used the same matches to tell the generator which
sign the figure showed; it was wrong on 2 of 3 clusters tried and the model followed
it into confidently wrong explanations. Getting the order slightly wrong costs some
irrelevant statute in the prompt; getting an identification wrong costs a wrong
answer. Only the first is acceptable, so only the first is done here.

Ranges are inclusive and written as they read in the statute: "84-103" is the
Regolamento's run of segnali di pericolo, article by article.

CORRECTING THIS FILE IS EXPECTED
--------------------------------
It was drafted from the article rubrics, not from teaching experience. A wrong entry
does not produce a wrong explanation so much as a vague one — the model is told to
refuse rather than guess when the article in front of it does not settle the
question, so a bad mapping shows up as an unusable draft, not a confident error.
"""

from __future__ import annotations

import json
import re
import unicodedata

from shared.config import CONTENT_OUT

NORMS_DIR = CONTENT_OUT / "norms"
FILES = {"cds": "cds.json", "reg": "regolamento.json"}
LABEL = {"cds": "Codice della Strada", "reg": "Regolamento di esecuzione"}
CITE = {"cds": "art. {n} C.d.S.", "reg": "art. {n} Reg."}

# Keyed by the opening segment of the ministerial topic name — the part before the
# first ";", which is what the reports already use as the short label. Matching is
# by prefix and `articles_for` fails loudly if a topic matches none or several, so
# a listato reissue that renames a topic breaks visibly rather than silently
# grounding it on nothing.
TOPIC_ARTICLES: dict[str, tuple[tuple[str, str], ...]] = {
    # --- signage: the Regolamento defines the signs, the Codice only frames them
    "Segnali di pericolo": (("reg", "84-103"), ("cds", "39"), ("reg", "77-82")),
    "Segnali di precedenza": (("reg", "104-114"), ("cds", "145"), ("cds", "39")),
    "Segnali di divieto": (("reg", "115-120"), ("cds", "39"), ("reg", "104")),
    "Segnali di obbligo": (("reg", "121-123"), ("cds", "39"), ("reg", "104")),
    "Segnali di indicazione": (("reg", "124-136"), ("cds", "39")),
    "Pannelli integrativi dei segnali": (("reg", "83"), ("cds", "39"), ("reg", "77-82")),
    "Segnali complementari": (
        ("cds", "42"), ("reg", "172-180"),      # delineatori, isole, rallentatori
        ("cds", "21"), ("reg", "30-43"),        # cantieri e segnalamento temporaneo
    ),
    "Segnaletica orizzontale": (("reg", "137-155"), ("cds", "40")),
    "Segnalazioni semaforiche": (
        ("reg", "156-171"), ("cds", "41"),      # lanterne semaforiche
        ("reg", "181-183"), ("cds", "43"),      # segnalazioni degli agenti
    ),

    # --- behaviour: the Codice, and the Regolamento only where it details a device
    "Esempi di precedenza": (
        ("cds", "145"), ("cds", "143"), ("cds", "154"), ("reg", "104-114"),
    ),
    "Norme sulla circol. dei veicoli": (
        ("cds", "140"), ("cds", "143-145"), ("cds", "154"),
        ("cds", "163"),                          # cortei, convogli militari
        ("cds", "177"),                          # veicoli di polizia e di emergenza
    ),
    "Norme sul sorpasso": (("cds", "148"), ("cds", "143"), ("cds", "149")),
    "Distanza di sicurezza": (("cds", "149"), ("cds", "141"), ("cds", "142")),
    "Limiti di velocità": (
        ("cds", "141-142"), ("cds", "146"),
        ("cds", "147"), ("reg", "184-186"),      # passaggi a livello
    ),
    "Fermata, sosta, arresto e partenza": (
        ("cds", "157-159"), ("reg", "120"), ("reg", "149"), ("cds", "175-176"),
    ),
    "Ingombro della carreggiata": (
        ("cds", "161-162"), ("cds", "164-165"), ("cds", "167"), ("cds", "169-170"),
        ("cds", "175-176"), ("cds", "61-62"),
    ),
    "Uso delle luci": (("cds", "151-153"), ("cds", "156"), ("cds", "72")),
    "Comportamenti per prevenire incidenti stradali": (
        ("cds", "140-141"), ("cds", "189"), ("cds", "170-171"),
    ),
    "Guida in relazione alle qualità e condizioni fisiche e psichiche": (
        ("cds", "186-187"), ("cds", "119"), ("cds", "173"), ("cds", "189"),
    ),

    # --- vehicle, paperwork and liability
    "Definizioni stradali e di traffico": (
        ("cds", "2-3"), ("cds", "46-47"), ("cds", "140"),
        ("cds", "190-191"), ("cds", "182"),      # pedoni e utenti deboli
    ),
    "Dispositivi di equipaggiamento: funzione ed uso": (
        ("cds", "72"), ("cds", "171-172"), ("cds", "162"),
    ),
    "Elementi costitutivi del veicolo importanti per la sicurezza": (
        ("cds", "71-72"), ("cds", "79-80"), ("cds", "141"),
    ),
    "Patenti di guida": (
        ("cds", "115-117"), ("cds", "119-120"), ("cds", "125-126"),
        ("cds", "128-130"), ("cds", "173"), ("cds", "180"),
    ),
    "Responsabilità civile, penale, amministrativa": (
        ("cds", "189"), ("cds", "193"), ("cds", "196"), ("cds", "200-202"),
    ),
    "Limitazione dei consumi": (("cds", "155"), ("cds", "79-80"), ("cds", "7")),
}


def expand(spec: str) -> list[str]:
    """"84-103" -> ["84", ..., "103"]. A bare number stays itself."""
    if "-" not in spec:
        return [spec]
    first, last = spec.split("-", 1)
    return [str(n) for n in range(int(first), int(last) + 1)]


def articles_for(topic_name: str) -> list[tuple[str, str]]:
    """[(source, article number)] for a ministerial topic, in citation order."""
    matches = [key for key in TOPIC_ARTICLES if topic_name.startswith(key)]
    if len(matches) != 1:
        raise KeyError(
            f"topic {topic_name.split(';')[0]!r} matches {len(matches)} entries in "
            f"TOPIC_ARTICLES ({matches}) — the listato renamed a topic, or the table "
            f"is incomplete. Fix content/articles.py rather than guessing at runtime."
        )
    return [(source, number)
            for source, spec in TOPIC_ARTICLES[matches[0]]
            for number in expand(spec)]


def load_corpus() -> dict[str, dict[str, dict]]:
    """{source: {article number: article}}. Raises if the corpus is not fetched."""
    corpus: dict[str, dict[str, dict]] = {}
    for source, filename in FILES.items():
        path = NORMS_DIR / filename
        if not path.exists():
            raise SystemExit(
                f"ERROR: {path} is missing — run content/fetch_norms.py --source both"
            )
        data = json.loads(path.read_text(encoding="utf-8"))
        corpus[source] = {a["number"]: a for a in data["articles"]}
    return corpus


def sign_index(corpus: dict[str, dict[str, dict]]) -> dict[str, dict]:
    """folded sign name -> {name, plate, articles}.

    Built from the Regolamento only; the Codice names no signs — verified, and the
    reason `--reparse` took the CdS from 207 "sign names" to 0.

    Requires the corpus to carry the `signs` field rather than re-deriving it here.
    Re-deriving would mean importing the extraction regex from `content/fetch_norms.py`,
    and nothing under `api/` should depend on the one-off pipeline; a second copy of the
    regex would drift from the first. Failing loudly with the command to run is better
    than either.
    """
    index: dict[str, dict] = {}
    for number, article in sorted(corpus["reg"].items()):
        signs = article.get("signs")
        if signs is None:
            raise SystemExit(
                "ERROR: the saved corpus predates the `signs` field — run\n"
                "  python content/fetch_norms.py --source both --reparse\n"
                "which recomputes it from the text already on disk, without refetching."
            )
        for name, plate in signs.items():
            entry = index.setdefault(
                fold(name), {"name": name, "plate": plate, "articles": []}
            )
            if ("reg", number) not in entry["articles"]:
                entry["articles"].append(("reg", number))
    return index


def fold(text: str) -> str:
    """Compare sign names to statement wording without punctuation or accents."""
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", text)).strip()


def matched_signs(statements: list[str], index: dict[str, dict]) -> list[dict]:
    """The index entries whose sign name appears in the cluster's own wording.

    Longest name first, so DIVIETO DI SORPASSO wins over DIVIETO. Every statement in
    a figure cluster is about the same sign, so a single naming statement is enough
    to place the whole cluster — which is the only reason this recovers anything at
    all, most statements saying only "il segnale raffigurato".
    """
    folded = [fold(s) for s in statements]
    return [index[name] for name in sorted(index, key=len, reverse=True)
            if any(name in s for s in folded)]


def signs_in(statements: list[str], index: dict[str, dict]) -> list[tuple[str, str]]:
    """Articles for whichever signs the cluster's own wording names, most specific first."""
    found: list[tuple[str, str]] = []
    for entry in matched_signs(statements, index):
        for reference in entry["articles"]:
            if reference not in found:
                found.append(reference)
    return found


def cite(source: str, number: str) -> str:
    return CITE[source].format(n=number)
