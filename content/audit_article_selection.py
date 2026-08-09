"""Which stored explanations were generated from the WRONG statute?

Cluster 638 was serving a flatly wrong rule because one long sign-name match ate the whole
prompt budget and pushed every article of its own topic out. The selector is fixed; this
finds the rows that were written before it was.

THE TEST IS NOT "IS THE TEXT WRONG"

Nobody can judge that mechanically, and reading 22 explanations is a human job. What CAN be
checked is whether the model was shown the right law, and there are three signals:

  1. DISPLACED — the fixed selector chooses a materially different article set from the old
     one. That means the stored text was written from a prompt the code would no longer
     produce, which is exactly cluster 638's failure.
  2. UNGROUNDED CITATION — the article the explanation cites is not among the articles it
     was given. Either it was quoting from memory or the citation is invented; both are
     reasons to regenerate.
  3. DISPUTED — the model contradicted the ministerial answer key. Already recorded, and
     the strongest single hint that something upstream is wrong.

A row flagged here is a row to REGENERATE, not proof that its text is wrong. The point is to
narrow 22 rows down to the few worth a human's attention.

Read-only. Usage:  python content/audit_article_selection.py
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone

from sqlalchemy import select

from api.models import Explanation
from api.services import explanations
from api.services.articles import articles_for, signs_in
from shared.constants import LANG_IT
from shared.db import async_session_factory

CITE_RE = re.compile(r"art(?:icolo)?\.?\s*(\d+[a-z\-]*)", re.I)

# When `select_articles` gained its reserved floor. Anything generated at or after this was
# written from the corrected prompt, whatever its topic was once exposed to.
FIXED_AT = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)


def expand_primary(topic_name: str) -> list[tuple[str, str]]:
    """The topic's FIRST mapped range — the articles that define its subject.

    TOPIC_ARTICLES lists the governing range first and the general Codice frame after, so
    losing the first entry is losing the law the cluster is about, while losing the tail is
    losing context. Only the first is a reason to regenerate.
    """
    from api.services.articles import TOPIC_ARTICLES, expand

    matches = [k for k in TOPIC_ARTICLES if topic_name.startswith(k)]
    if len(matches) != 1:
        return []
    source, spec = TOPIC_ARTICLES[matches[0]][0]
    return [(source, number) for number in expand(spec)]


def old_selection(topic, statements, corpus, index, budget):
    """What the selector chose BEFORE the reservation — matches first, one shared budget."""
    ordered = []
    for reference in signs_in(statements, index) + articles_for(topic):
        if reference not in ordered:
            ordered.append(reference)
    chosen, used = [], 0
    for source, number in ordered:
        article = corpus[source].get(number)
        if article is None:
            continue
        if used and used + len(article["text"]) > budget:
            continue
        chosen.append((source, number))
        used += len(article["text"])
    return chosen


async def main() -> None:
    corpus, index = explanations.corpus_and_index()
    factory = async_session_factory()

    async with factory() as session:
        rows = list(await session.scalars(
            select(Explanation).where(Explanation.lang == LANG_IT)
            .order_by(Explanation.cluster_id)))

        print(f"auditing {len(rows)} Italian explanations\n")
        suspect, clean = [], []

        for row in rows:
            topic, members, _image = await explanations.cluster_members(
                session, row.cluster_id)
            if not members:
                continue
            judged = explanations.sample_statements(members)
            statements = [m["statement"] for m in judged]

            now = [(a["source"], a["number"]) for a in explanations.select_articles(
                topic, statements, corpus, index)]
            before = old_selection(topic, statements, corpus, index,
                                   explanations.CONTEXT_CHARS)

            gained = [a for a in now if a not in before]
            reasons = []
            severity = 0

            # SEVERITY IS THE WHOLE POINT. "An article was added" fires for harmless tail
            # articles too — the Codice frame that only ever provides context. What broke
            # cluster 638 was losing the topic's PRIMARY range, the articles that actually
            # define the thing being explained, which is the first entry in TOPIC_ARTICLES.
            primary = set(expand_primary(topic))
            lost_primary = [a for a in gained if a in primary]
            if lost_primary:
                severity = 2
                reasons.append(
                    "MISSING ITS OWN SUBJECT: was never shown "
                    + ", ".join(f"{s}.{n}" for s, n in lost_primary[:6]))
            elif gained:
                severity = 1
                reasons.append(
                    "minor: would now also see "
                    + ", ".join(f"{s}.{n}" for s, n in gained[:4]))

            cited = {m.group(1) for m in CITE_RE.finditer(row.text)}
            given = {n for _s, n in before}
            ungrounded = sorted(cited - given)
            if ungrounded:
                reasons.append(f"cites art. {', '.join(ungrounded)} — not in its prompt")

            if row.disputed:
                reasons.append(f"disputes the key on {len(row.disputed.split(','))} statement(s)")

            # A row REGENERATED since the selector was fixed is not suspect however
            # exposed its topic was — it was written from the corrected prompt. Without
            # this the audit reports the same clusters for ever, because it compares
            # selectors (a property of the topic) rather than rows.
            if row.generated_at and row.generated_at >= FIXED_AT:
                reasons = [r for r in reasons
                           if not r.startswith(("MISSING", "minor", "cites"))]
                severity = 0
                if row.disputed:
                    reasons.append(
                        f"regenerated since the fix, still disputes the key on "
                        f"{len(row.disputed.split(','))} statement(s) — worth READING")
                    severity = 1

            if row.disputed and severity == 0 and not reasons:
                severity = 1
            (suspect if reasons else clean).append((row, topic, reasons, severity))

        severe = [x for x in suspect if x[3] == 2]
        minor = [x for x in suspect if x[3] < 2]

        print(f"=== {len(severe)} SEVERE — generated without the articles that define "
              f"their own subject ===")
        for row, topic, reasons, _sev in severe:
            print(f"\n  cluster {row.cluster_id} [{row.status}] — {topic.split(';')[0][:52]}")
            for r in reasons:
                print(f"      · {r}")
            print(f"      {row.text[:110]}…")

        print(f"\n=== {len(minor)} MINOR — extra context only, or a disputed statement ===")
        for row, topic, reasons, _sev in minor:
            print(f"  {row.cluster_id:>5}  {'; '.join(reasons)[:96]}")

        print(f"\n=== {len(clean)} look sound ===")
        print("  " + ", ".join(str(r.cluster_id) for r, _t, _x, _s in clean))
        print(f"\nREGENERATE: {' '.join(str(x[0].cluster_id) for x in severe)}")
        print("\nSuspect means REGENERATE and then read, not that the text is wrong.")


asyncio.run(main())
