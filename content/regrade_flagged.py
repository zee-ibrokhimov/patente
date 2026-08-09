"""Re-judge already-flagged explanations against the grounding rule.

The numeric gate used to withhold any explanation containing a digit. It now checks whether
the number appears in the statute the model was shown. That change applies to new
generations; rows already written carry the old verdict and stay withheld for ever, which
means the learner keeps being told "not available" for text that is fine.

This re-runs ONLY the numeric part of the decision, from the stored text and the articles
`select_articles` derives for the cluster's topic — the same articles the model saw, because
that function is deterministic. No model calls, no new text: a row is either promoted from
flagged to draft or left exactly as it was.

Never DEMOTES. A row that is currently servable is not touched, whatever this thinks of it —
taking a working explanation away is the failure mode this repair exists to undo.

Never overrides the other gates. A row flagged for low confidence, for missing verdicts, or
for arguing against most of its cluster keeps that reason and stays withheld.

Usage:  python content/regrade_flagged.py [--apply]
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select

from api.models import Explanation
from api.services import explanations
from shared.constants import LANG_IT, STATUS_DRAFT, STATUS_FLAGGED
from shared.db import async_session_factory

APPLY = "--apply" in sys.argv

# Reasons that are nothing to do with numbers. If one of these is on the row, the row stays
# flagged no matter how well grounded its figures are.
OTHER_GATES = ("low confidence", "no verdict", "argues against")


async def main() -> None:
    corpus, index = explanations.corpus_and_index()
    factory = async_session_factory()

    async with factory() as session:
        rows = list(await session.scalars(
            select(Explanation).where(Explanation.status == STATUS_FLAGGED)))
        clusters = sorted({r.cluster_id for r in rows})
        print(f"{len(rows)} flagged rows across {len(clusters)} clusters\n")

        promoted = 0
        for cluster_id in clusters:
            italian = await session.scalar(
                select(Explanation).where(Explanation.cluster_id == cluster_id,
                                          Explanation.lang == LANG_IT))
            if italian is None:
                print(f"  cluster {cluster_id}: no Italian row, skipping")
                continue

            # ONLY when the Italian itself is flagged.
            #
            # A cluster can hold rows from DIFFERENT generations: cluster 306's it/ru/en
            # were restored from a backup after a bad regeneration, while its uz row came
            # from that bad roll. The gate reads the Italian, so judging a stray Uzbek row
            # by a different generation's Italian would promote text this rule never saw.
            #
            # Caught by the dry run, which offered to promote exactly that.
            if italian.status != STATUS_FLAGGED:
                print(f"  cluster {cluster_id}: Italian is already {italian.status} — the "
                      f"flagged rows here are from another generation, leaving them")
                continue

            flags = italian.flags or ""
            if any(g in flags for g in OTHER_GATES):
                print(f"  cluster {cluster_id}: held by another gate, leaving it")
                print(f"      {flags[:110]}")
                continue

            topic, members, _image = await explanations.cluster_members(session, cluster_id)
            judged = explanations.sample_statements(members)
            grounded = explanations.select_articles(
                topic, [m["statement"] for m in judged], corpus, index)
            invented = explanations.ungrounded_numbers(italian.text, grounded)

            if invented:
                print(f"  cluster {cluster_id}: still withheld — {', '.join(invented)} "
                      f"not in the cited article")
                continue

            print(f"  cluster {cluster_id}: PROMOTE — every figure is in the statute")
            print(f"      {italian.text[:120]}")
            promoted += 1
            if APPLY:
                for row in await session.scalars(
                        select(Explanation).where(Explanation.cluster_id == cluster_id)):
                    if row.status == STATUS_FLAGGED:
                        row.status = STATUS_DRAFT
                        row.flags = (row.flags or "") + " | regraded: numbers grounded"

        if APPLY:
            await session.commit()
        print(f"\n{promoted} cluster(s) {'promoted' if APPLY else 'would be promoted'}")
        if not APPLY:
            print("dry run — pass --apply to write")


asyncio.run(main())
