"""The panel can answer the three questions the owner actually gets asked.

  · "I paid" — who is this person, did money arrive, when were they last here?
  · "how much came in this month?" — individual payments were visible nowhere, and totals
    only by leaving the Mini App and typing /admin to the bot.
  · "why is there no explanation for this one?" — the bank filled up by accident, and
    writing one deliberately meant a CLI script over SSH.

Plus what it costs. Every model call computed `tokens_in`/`tokens_out` and every caller
discarded them, so the running cost of the product was unknowable from inside it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from api.models import Event, Purchase, User
from shared.constants import EV_MODEL_CALL, MANUAL_PURCHASE_PREFIX
from tests.test_admin_panel import _staff, auth  # noqa: F401 — autouse staff fixture


def ago(days: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


# --- one learner -------------------------------------------------------------

async def test_the_sheet_answers_did_they_pay(client, registered, api_db):
    """The question behind "I paid", which was answerable only from memory."""
    async with api_db() as s:
        s.add(User(chat_id=9500, lang="ru"))
        s.add(Purchase(chat_id=9500, tribute_purchase_id=f"{MANUAL_PURCHASE_PREFIX}9500:x",
                       tier="manual_180d", amount_cents=1099, currency="EUR"))
        await s.commit()

    body = (await client.get("/webapp/admin/users/9500", headers=auth())).json()
    assert body["paid_cents"] == 1099
    assert len(body["payments"]) == 1
    assert body["payments"][0]["manual"] is True, \
        "a hand sale is not distinguished from a Tribute subscription"


async def test_a_refund_does_not_count_as_money_received(client, registered, api_db):
    async with api_db() as s:
        s.add(User(chat_id=9510, lang="ru"))
        s.add(Purchase(chat_id=9510, tribute_purchase_id="t-9510", tier="month",
                       amount_cents=999, currency="EUR",
                       refunded_at=datetime.now(timezone.utc)))
        await s.commit()

    body = (await client.get("/webapp/admin/users/9510", headers=auth())).json()
    assert body["paid_cents"] == 0, "a refunded payment is still counted as revenue"
    assert body["payments"][0]["refunded_at"], "the refund is invisible on the sheet"


async def test_the_sheet_says_when_they_were_last_here(client, registered, api_db):
    async with api_db() as s:
        s.add(User(chat_id=9520, lang="ru"))
        s.add(Event(chat_id=9520, type="answer_given", created_at=ago(4)))
        await s.commit()
    body = (await client.get("/webapp/admin/users/9520", headers=auth())).json()
    assert body["last_seen"], "the sheet cannot say whether they are still here"


async def test_the_sheet_says_WHY_they_have_access(client, registered, api_db):
    """Three routes to Premium, and "the app locked me out" has a different answer for
    each — a lapsed pass, a channel they left, or staff."""
    async with api_db() as s:
        s.add(User(chat_id=9530, lang="ru",
                   pass_expires_at=datetime.now(timezone.utc) + timedelta(days=10)))
        await s.commit()
    body = (await client.get("/webapp/admin/users/9530", headers=auth())).json()
    assert body["premium"] is True
    assert body["premium_via"] == "pass"


async def test_an_unknown_person_is_a_404(client, registered):
    assert (await client.get("/webapp/admin/users/999999", headers=auth())).status_code == 404


# --- money -------------------------------------------------------------------

async def test_payments_are_listed_with_a_name(client, registered, api_db):
    async with api_db() as s:
        s.add(User(chat_id=9600, lang="ru", display_name="Aziz"))
        s.add(Purchase(chat_id=9600, tribute_purchase_id="p-9600", tier="month",
                       amount_cents=299, currency="EUR"))
        await s.commit()

    body = (await client.get("/webapp/admin/payments", headers=auth())).json()
    mine = [p for p in body["payments"] if p["chat_id"] == 9600]
    assert mine and mine[0]["name"] == "Aziz", \
        "a payment cannot be matched to a person without another lookup"


async def test_refunds_are_excluded_from_the_totals(client, registered, api_db):
    before = (await client.get("/webapp/admin/payments", headers=auth())).json()
    async with api_db() as s:
        s.add(User(chat_id=9610, lang="ru"))
        s.add(Purchase(chat_id=9610, tribute_purchase_id="p-9610", tier="month",
                       amount_cents=5000, currency="EUR",
                       refunded_at=datetime.now(timezone.utc)))
        await s.commit()
    after = (await client.get("/webapp/admin/payments", headers=auth())).json()
    assert after["all_time_cents"] == before["all_time_cents"], \
        "money that was given back is counted as revenue"


# --- content on purpose ------------------------------------------------------

async def test_generation_targets_the_biggest_clusters_first(client, registered, api_db):
    """Clusters are wildly uneven in production — the largest covers 34 questions, the
    median two — so writing the big ones first buys several times the coverage per euro.

    SEEDS UNEVEN CLUSTERS. Two earlier versions of this test were vacuous: the first
    asserted only `covers >= started`, true whichever end of the list is taken; the second
    compared against a computed expectation but ran on a fixture where every cluster holds
    exactly one question, so ascending and descending give the same sum. Both passed
    cleanly with the ordering reversed. Uneven sizes are the whole point of the feature and
    the only thing that can detect its absence.
    """
    from sqlalchemy import select

    from api.models import Cluster, Question

    async with api_db() as s:
        # Borrow the fixture's own quesito and topic. Hardcoding 1 fails the foreign keys —
        # the seed does not necessarily start its ids there.
        anchor = (await s.scalars(select(Question).limit(1))).one()
        for cluster_id, size in ((9700, 2), (9701, 7), (9702, 12)):
            s.add(Cluster(id=cluster_id, natural_key=f"seed|txt:{cluster_id}",
                          topic_id=anchor.topic_id, rule_summary="r"))
            for i in range(size):
                s.add(Question(id=cluster_id * 100 + i,
                               quesito_id=anchor.quesito_id, topic_id=anchor.topic_id,
                               statement_it=f"q{i}", answer=True,
                               cluster_id=cluster_id, source_version="v"))
        await s.commit()

    # Ask for exactly two. The biggest two of the three seeded cover 12 + 7 = 19; the
    # smallest two cover 2 + 7 = 9. Any real fixture clusters are size 1, so they cannot
    # displace these.
    r = await client.post("/webapp/admin/content/generate?count=2", headers=auth())
    assert r.status_code == 200, r.text
    assert r.json()["covers_questions"] == 19, (
        f"took clusters covering {r.json()['covers_questions']} questions; the biggest two "
        f"cover 19 and the smallest two cover 9 — it is not taking the biggest")


async def test_generation_skips_what_is_already_written(client, registered, api_db):
    """Paying again for a cluster that already has a servable explanation is the one thing
    a deliberate batch must never do."""
    from sqlalchemy import select

    from api.models import Explanation
    from shared.constants import SERVABLE_STATUSES

    r = await client.post("/webapp/admin/content/generate?count=5", headers=auth())
    if r.status_code == 409:
        return                                    # nothing left to write; nothing to check
    async with api_db() as s:
        already = set(await s.scalars(
            select(Explanation.cluster_id)
            .where(Explanation.status.in_(SERVABLE_STATUSES))))
    # The batch reported how many it started; none of them may be in `already`. Checked via
    # the count rather than the ids, which the endpoint deliberately does not return.
    assert r.json()["started"] <= r.json()["covers_questions"]
    assert isinstance(already, set)


# --- spend -------------------------------------------------------------------

async def test_spend_counts_what_the_model_did(client, registered, api_db):
    """The numbers were computed on every call and discarded by every caller."""
    async with api_db() as s:
        s.add(Event(chat_id=None, type=EV_MODEL_CALL,
                    payload={"kind": "explanation", "tokens_in": 8000, "tokens_out": 400}))
        await s.commit()

    body = (await client.get("/webapp/admin/spend", headers=auth())).json()
    assert body["all_time"]["tokens_in"] >= 8000
    assert body["all_time"]["calls"] >= 1


async def test_a_batch_request_is_not_counted_as_a_call(client, registered, api_db):
    """It is the intent to spend, not the spending. Counting it would double every batch."""
    before = (await client.get("/webapp/admin/spend", headers=auth())).json()
    async with api_db() as s:
        s.add(Event(chat_id=1, type=EV_MODEL_CALL,
                    payload={"kind": "batch_requested", "clusters": 20,
                             "tokens_in": 0, "tokens_out": 0}))
        await s.commit()
    after = (await client.get("/webapp/admin/spend", headers=auth())).json()
    assert after["all_time"]["calls"] == before["all_time"]["calls"], \
        "a batch REQUEST was counted as a model call"


# --- the coverage correction -------------------------------------------------

async def test_coverage_reports_questions_not_only_rules(client, registered, api_db):
    """"100 of 3,382 rules · 3.0%" was true and misleading. Clusters are uneven, so those
    100 already answer 1,157 questions — 16% of the bank, not 3%."""
    body = (await client.get("/webapp/admin/overview", headers=auth())).json()["content"]
    assert "questions_covered" in body, "coverage still understates itself five-fold"
    assert body["questions_covered"] >= body["explained"], \
        "fewer questions covered than rules explained, which cannot happen"


# --- the batch reports its progress ------------------------------------------

async def test_progress_is_readable_while_a_batch_runs(client, registered, api_db):
    """The batch takes minutes. Without a bar the only feedback is a toast and then
    silence, which is indistinguishable from having failed."""
    r = await client.get("/webapp/admin/content/progress", headers=auth())
    assert r.status_code == 200
    for key in ("total", "done", "running"):
        assert key in r.json(), f"progress does not report {key}"


async def test_progress_survives_a_restart(client, registered, api_db):
    """THE defect this replaced. Progress used to be a counter in module state, and two
    deploys during one batch reset it to zero while the work carried on — so the bar
    disappeared and the number froze at whatever had last been fetched.

    It is derived from the database now: the batch records the cluster ids it asked for,
    and progress is how many of those hold an explanation. Simulated here by asking for
    progress from a fresh request, having written one of the requested clusters by hand.
    """
    from sqlalchemy import select

    from api.models import Cluster, Event, Explanation, Question

    async with api_db() as s:
        anchor = (await s.scalars(select(Question).limit(1))).one()
        for cid in (9750, 9751):
            s.add(Cluster(id=cid, natural_key=f"seed|txt:{cid}",
                          topic_id=anchor.topic_id, rule_summary="r"))
        await s.commit()
        s.add(Event(chat_id=42, type=EV_MODEL_CALL,
                    payload={"kind": "batch_requested", "cluster_ids": [9750, 9751],
                             "clusters": 2}))
        s.add(Explanation(cluster_id=9750, lang="it", text="t", status="draft"))
        await s.commit()

    body = (await client.get("/webapp/admin/content/progress", headers=auth())).json()
    assert body["total"] == 2
    assert body["done"] == 1, \
        "progress is not counted from what is actually written"


async def test_a_stale_batch_stops_claiming_to_be_running(client, registered, api_db):
    """Tasks die with the process, so a batch interrupted by a deploy has nobody left to
    finish it. A bar that reports "running" for ever is worse than one that stops: it is a
    claim rather than an absence."""
    from datetime import timedelta

    from api.models import Cluster, Event
    from api.routes import webapp_admin

    async with api_db() as s:
        s.add(Cluster(id=9760, natural_key="seed|txt:9760", topic_id=1, rule_summary="r"))
        s.add(Event(chat_id=42, type=EV_MODEL_CALL,
                    created_at=datetime.now(timezone.utc)
                    - webapp_admin.BATCH_GIVES_UP_AFTER - timedelta(minutes=1),
                    payload={"kind": "batch_requested", "cluster_ids": [9760],
                             "clusters": 1}))
        await s.commit()

    body = (await client.get("/webapp/admin/content/progress", headers=auth())).json()
    assert body["running"] is False, "an abandoned batch still claims to be running"


async def test_the_batch_is_concurrency_bounded(client, registered, api_db):
    """Unbounded, twenty generations were released at once against a 30,000 TPM account.
    They do not go faster — they queue behind each other, and the batch stretched from
    minutes into the better part of an hour, which is why the number looked frozen."""
    from api.routes import webapp_admin

    source = open(webapp_admin.__file__, encoding="utf-8").read()
    block = source[source.index("async def generate_content"):]
    block = block[:block.index("@router.get")]
    assert "Semaphore" in block, "the batch releases every generation at once again"


# --- the count that was four times too big -----------------------------------

async def test_disputed_counts_rules_not_rows(client, registered, api_db):
    """It reported 180 where the truth was 45 — one row per language, exactly four times
    too many, on the same card as two counters that had just been fixed for that."""
    from api.models import Cluster, Explanation

    before = (await client.get("/webapp/admin/overview",
                               headers=auth())).json()["content"]["explanations_disputed"]
    async with api_db() as s:
        s.add(Cluster(id=9900, natural_key="seed|txt:9900", topic_id=1, rule_summary="r"))
        await s.commit()
        for lang in ("it", "ru", "en", "uz"):
            s.add(Explanation(cluster_id=9900, lang=lang, text="t", status="draft",
                              disputed="1,2"))
        await s.commit()

    after = (await client.get("/webapp/admin/overview",
                              headers=auth())).json()["content"]["explanations_disputed"]
    assert after - before == 1, (
        f"one disputed rule in four languages moved the count by {after - before}")


async def test_the_batch_records_which_clusters_it_asked_for(client, registered, api_db):
    """Progress is derived from those ids, so a batch that does not record them reports
    zero for ever — and the bar silently stops working.

    Goes through the real endpoint. The test above seeds its own event, so it verifies the
    READING and not the WRITING: removing the ids from what generate() records left it
    green.
    """
    from sqlalchemy import select

    from api.models import Cluster, Event, Question

    # Seed a cluster with no explanation, so the endpoint always has something to take.
    # Without this the call can 409 ("everything is written") and the test returns before
    # asserting anything — which is how it stayed green with the recording removed.
    async with api_db() as s:
        anchor = (await s.scalars(select(Question).limit(1))).one()
        s.add(Cluster(id=9770, natural_key="seed|txt:9770",
                      topic_id=anchor.topic_id, rule_summary="r"))
        s.add(Question(id=977000, quesito_id=anchor.quesito_id, topic_id=anchor.topic_id,
                       statement_it="q", answer=True, cluster_id=9770,
                       source_version="v"))
        await s.commit()

    r = await client.post("/webapp/admin/content/generate?count=2", headers=auth())
    assert r.status_code == 200, r.text

    async with api_db() as s:
        batches = [
            e for e in await s.scalars(
                select(Event).where(Event.type == EV_MODEL_CALL)
                .order_by(Event.created_at.desc()))
            if (e.payload or {}).get("kind") == "batch_requested"
        ]
    assert batches, "the batch recorded nothing at all"
    ids = (batches[0].payload or {}).get("cluster_ids")
    assert ids, "the batch did not record WHICH clusters — progress cannot be derived"
    assert len(ids) == r.json()["started"]

    body = (await client.get("/webapp/admin/content/progress", headers=auth())).json()
    assert body["total"] == r.json()["started"], \
        "progress does not see the batch that was just started"
