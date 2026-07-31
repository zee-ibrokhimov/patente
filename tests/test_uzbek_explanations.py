"""An Uzbek reader gets an Uzbek explanation.

Reported by the owner, 2026-07-31: "uzbek explanation is in russian it should be in
dedicated lang in our case uzbek".

They were right, and it was deliberate. Uzbek shipped as UI + question translations with
`EXPLANATION_FALLBACK = {uz: ru}`, on the reasoning that a bad translation sits under the
Italian where a reader can see it is off, while a bad explanation is the only text on
screen and is the thing being sold — so Uzbek would get translations first and explanations
once there was a way to check them.

What that produced in practice: someone sets the app to Uzbek, gets Uzbek questions, Uzbek
vocabulary, an Uzbek interface — and then the one screen they have to READ, the paid one,
arrives in Russian with no warning. It also asks the people who chose Uzbek over Russian to
use Russian for the feature they paid for.

THREE HALVES, AND ONLY ONE OF THEM IS THE LIST
----------------------------------------------
Adding "uz" to EXPLANATION_LANGUAGES changes nothing on its own. The model is only asked
for the languages named in the prompt, so the list would advertise a language the call
never returns, and `ensure` would regenerate the cluster on every Uzbek request forever
looking for a row that cannot appear. So:

  · the prompt asks for Uzbek, in Latin script, and is told to OMIT the key rather than
    guess — a wrong language is worse than a missing one;
  · `deliver` tries the reader's own language first and reaches for the fallback per
    CLUSTER, not per audience;
  · a language that does not come back is remembered for an hour, exactly as translations
    already do, so one stubborn cluster is not billed on every request.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update as sa_update

from api.models import Explanation, Question, User
from api.services import explanations
from api.services.entitlement import Access, Entitlement
from shared.constants import (
    EXPLANATION_LANGUAGES,
    LANG_RU,
    LANG_UZ,
    STATUS_DRAFT,
    STATUS_FLAGGED,
)

from tests.test_explanation_service import FakeClient, REPLY


@pytest.fixture(autouse=True)
def _clean_state():
    explanations._locks.clear()
    explanations._missing.clear()
    yield
    explanations._locks.clear()
    explanations._missing.clear()


@pytest.fixture
def fake_openai(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(explanations, "openai_client", lambda: client)
    monkeypatch.setattr(
        explanations, "select_articles",
        lambda *a, **k: [{"source": "reg", "number": "106", "rubric": "Dare precedenza",
                          "text": "1. Il segnale DARE PRECEDENZA deve essere usato…"}],
    )
    monkeypatch.setattr(explanations, "corpus_and_index", lambda: ({"reg": {}, "cds": {}}, {}))
    return client


@pytest.fixture
async def cluster(api_db, tmp_path, monkeypatch):
    """Cluster 1 with its explanations cleared, so the next request is a cache miss."""
    async with api_db() as s:
        for row in (await s.scalars(select(Explanation))).all():
            await s.delete(row)
        await s.commit()
    images = tmp_path / "images"
    images.mkdir(parents=True, exist_ok=True)
    (images / "sign_a.jpeg").write_bytes(b"\xff\xd8\xff\xe0 not a real jpeg \xff\xd9")
    monkeypatch.setattr(explanations, "CONTENT_OUT", tmp_path)
    return 1


async def deliver_as(api_db, lang: str, *, generate=True):
    """Ask for question 1's explanation as a Premium reader with this UI language."""
    async with api_db() as s:
        await s.execute(sa_update(User).where(User.chat_id == 42).values(lang=lang))
        await s.commit()
    async with api_db() as s:
        user = await s.get(User, 42)
        question = await s.get(Question, 1)
        payload, access = await explanations.deliver(
            s, question, user, _premium(), generate_if_missing=generate)
    return payload, access


def _premium() -> Entitlement:
    """A reader who is entitled to explanations, however they got there."""
    return Entitlement(has_pass=True, pass_expires_at=None, free_explanations_left=0)


# --- the report -------------------------------------------------------------

async def test_an_uzbek_reader_gets_uzbek(api_db, registered, fake_openai, cluster):
    """THE bug. This returned Russian text and `explanation_lang: "ru"`."""
    payload, access = await deliver_as(api_db, LANG_UZ)
    assert access is Access.SHOWN
    assert payload["explanation_lang"] == LANG_UZ, "an Uzbek reader was served another language"
    assert payload["explanation"].startswith("YO'L BERING")


async def test_the_uzbek_row_is_actually_stored(api_db, registered, fake_openai, cluster):
    await deliver_as(api_db, LANG_UZ)
    async with api_db() as s:
        langs = {e.lang for e in (await s.scalars(
            select(Explanation).where(Explanation.cluster_id == 1))).all()}
    assert LANG_UZ in langs
    assert langs == set(EXPLANATION_LANGUAGES)


async def test_uzbek_costs_no_extra_call(api_db, registered, fake_openai, cluster):
    """One call returns every language. Serving Uzbek must not double the bill."""
    await deliver_as(api_db, LANG_UZ)
    await deliver_as(api_db, LANG_RU)
    assert fake_openai.calls == 1


# --- the fallback, demoted from policy to safety net ------------------------

async def test_russian_is_used_only_when_this_cluster_has_no_uzbek(
        api_db, registered, fake_openai, cluster):
    """The model omitted "uz" — which the prompt explicitly permits, because a wrong
    language is worse than a missing one. Showing Russian beats showing nothing, and the
    reader is told which language they got."""
    fake_openai.reply = {**REPLY, "spiegazione": {
        k: v for k, v in REPLY["spiegazione"].items() if k != LANG_UZ}}

    payload, access = await deliver_as(api_db, LANG_UZ)
    assert access is Access.SHOWN
    assert payload["explanation_lang"] == LANG_RU
    assert payload["explanation"].startswith("Знак")


async def test_the_fallback_is_per_cluster_not_per_user(api_db, registered, fake_openai, cluster):
    """A reader who fell back on one cluster must still get Uzbek on the next. The old
    code decided by language before it ever looked at a row, so this could not happen."""
    fake_openai.reply = {**REPLY, "spiegazione": {
        k: v for k, v in REPLY["spiegazione"].items() if k != LANG_UZ}}
    first, _ = await deliver_as(api_db, LANG_UZ)
    assert first["explanation_lang"] == LANG_RU

    # A different cluster, and this time the model writes Uzbek.
    async with api_db() as s:
        await s.execute(sa_update(Question).where(Question.id == 1).values(cluster_id=2))
        await s.commit()
    fake_openai.reply = REPLY
    explanations._missing.clear()

    second, _ = await deliver_as(api_db, LANG_UZ)
    assert second["explanation_lang"] == LANG_UZ


async def test_nothing_servable_in_either_language_is_still_unavailable(
        api_db, registered, fake_openai, cluster):
    """The fallback must not turn a withheld cluster into a served one."""
    fake_openai.reply = {**REPLY, "spiegazione": {**REPLY["spiegazione"],
                                                  "it": "Il limite è di 90 km/h."}}
    payload, access = await deliver_as(api_db, LANG_UZ)
    assert access is Access.UNAVAILABLE
    assert payload["explanation"] is None


# --- the cost hole that adding a language opens -----------------------------

async def test_a_cluster_with_no_uzbek_is_not_regenerated_every_time(
        api_db, registered, fake_openai, cluster):
    """THE cost bug, and the reason `ensure` is keyed on the requested language. Without
    the ceiling, every Uzbek reader of this cluster pays for a call that cannot produce
    the row they are waiting for — the exact defect already fixed in translations."""
    fake_openai.reply = {**REPLY, "spiegazione": {
        k: v for k, v in REPLY["spiegazione"].items() if k != LANG_UZ}}

    async with api_db() as s:
        for _ in range(4):
            assert await explanations.ensure(s, 1, LANG_UZ) is None
    assert fake_openai.calls == 1, \
        f"a missing language was regenerated {fake_openai.calls} times"


async def test_a_missing_uzbek_does_not_block_russian(api_db, registered, fake_openai, cluster):
    """The miss is keyed on (cluster, language). Remembering "no Uzbek here" must not
    withhold the Russian row that the same call produced perfectly well."""
    fake_openai.reply = {**REPLY, "spiegazione": {
        k: v for k, v in REPLY["spiegazione"].items() if k != LANG_UZ}}
    async with api_db() as s:
        assert await explanations.ensure(s, 1, LANG_UZ) is None
        assert await explanations.ensure(s, 1, LANG_RU) is not None


async def test_a_failure_on_one_cluster_does_not_disable_uzbek_everywhere(
        api_db, registered, fake_openai, cluster):
    """Marking a whole LANGUAGE as broken because one cluster failed would turn a single
    bad row into an outage for that audience."""
    explanations._missing[(1, LANG_UZ)] = 9e9
    async with api_db() as s:
        assert await explanations.ensure(s, 2, LANG_UZ) is not None


def test_the_ceiling_expires():
    """An hour later it is worth another try: a model that would not write Uzbek once may
    write it next time. This is a cost guard, not a permanent verdict about a cluster."""
    explanations._missing[(1, LANG_UZ)] = 0.0
    assert explanations._recently_failed(1, LANG_UZ, now=10.0) is True
    assert explanations._recently_failed(
        1, LANG_UZ, now=explanations.RETRY_AFTER + 10) is False


def test_a_cluster_never_asked_for_is_not_marked_failed():
    assert explanations._recently_failed(4242, LANG_UZ) is False


# --- a regeneration must not cost the other languages anything --------------

async def test_adding_uzbek_does_not_revoke_a_servable_russian_explanation(
        api_db, registered, fake_openai, cluster):
    """Found by running the backfill against PRODUCTION, not by reading the code.

    Most regenerations now exist to fill in a missing language, and `generate` re-rolls the
    whole cluster: new text, new gates, new status for every language. The gates are partly
    luck — the numeric one fires on any digit in the fresh Italian — so a cluster that was
    `draft` can come back `flagged`.

    Cluster 306 did exactly that. Servable in it/ru/en, an Uzbek row was requested, the new
    Italian said "M1" where the old had not, and all four languages became `flagged`. One
    Uzbek reader asking one question silently revoked a working explanation for every
    Russian and English reader of that cluster. Servable Italian rows went 11 to 10 and
    nothing reported it.
    """
    # First roll: clean, so it/ru/en are servable and there is no Uzbek yet.
    fake_openai.reply = {**REPLY, "spiegazione": {
        k: v for k, v in REPLY["spiegazione"].items() if k != LANG_UZ}}
    async with api_db() as s:
        assert await explanations.ensure(s, 1, LANG_RU) is not None
        before = {e.lang: (e.status, e.text) for e in (await s.scalars(
            select(Explanation).where(Explanation.cluster_id == 1))).all()}
    assert before["ru"][0] == STATUS_DRAFT
    assert LANG_UZ not in before

    # Second roll, triggered by an Uzbek reader: this time the Italian trips a gate.
    explanations._missing.clear()
    fake_openai.reply = {**REPLY, "spiegazione": {
        **REPLY["spiegazione"], "it": "Il limite in autostrada è di 130 km/h."}}
    async with api_db() as s:
        await explanations.ensure(s, 1, LANG_UZ)
        after = {e.lang: (e.status, e.text) for e in (await s.scalars(
            select(Explanation).where(Explanation.cluster_id == 1))).all()}

    assert after["ru"] == before["ru"], "the Russian explanation was demoted or rewritten"
    assert after["en"] == before["en"], "the English explanation was demoted or rewritten"
    assert after["it"] == before["it"], "the Italian explanation was demoted or rewritten"


async def test_the_new_language_is_still_judged_on_its_own_roll(
        api_db, registered, fake_openai, cluster):
    """Protecting the other languages must NOT smuggle a withheld explanation through. The
    Uzbek row belongs to the bad roll and has to carry the bad roll's verdict."""
    fake_openai.reply = {**REPLY, "spiegazione": {
        k: v for k, v in REPLY["spiegazione"].items() if k != LANG_UZ}}
    async with api_db() as s:
        await explanations.ensure(s, 1, LANG_RU)

    explanations._missing.clear()
    fake_openai.reply = {**REPLY, "spiegazione": {
        **REPLY["spiegazione"], "it": "Il limite in autostrada è di 130 km/h."}}
    async with api_db() as s:
        row = await explanations.ensure(s, 1, LANG_UZ)
    assert row is not None
    assert row.status == STATUS_FLAGGED, "a gated roll was served anyway"

    # And the reader falls back rather than seeing it.
    payload, access = await deliver_as(api_db, LANG_UZ, generate=False)
    assert access is Access.SHOWN
    assert payload["explanation_lang"] == LANG_RU


async def test_a_regeneration_may_still_IMPROVE_a_withheld_row(
        api_db, registered, fake_openai, cluster):
    """The guard is one-directional. A cluster withheld by an unlucky roll must still be
    able to recover on a better one, or a single bad generation is permanent."""
    fake_openai.reply = {**REPLY, "spiegazione": {
        **REPLY["spiegazione"], "it": "Il limite in autostrada è di 130 km/h."}}
    async with api_db() as s:
        first = await explanations.ensure(s, 1, LANG_RU)
        assert first.status == STATUS_FLAGGED

    fake_openai.reply = REPLY
    async with api_db() as s:
        await explanations.generate(s, 1)
        rows = {e.lang: e.status for e in (await s.scalars(
            select(Explanation).where(Explanation.cluster_id == 1))).all()}
    assert rows["ru"] == STATUS_DRAFT, "a flagged cluster could never recover"


# --- warming, which decides whether Uzbek is fast or merely present ---------

async def test_serving_a_question_warms_uzbek_not_russian(
        api_db, client, registered, monkeypatch):
    """The other half of the fallback bug, and the one that would have survived the fix.

    The serve path resolved EXPLANATION_FALLBACK before warming — correct while Uzbek was
    unwritten, since warming "uz" paid for a row no read could find. Now it is backwards:
    on a cluster already cached in Russian, warming "ru" is an instant cache hit that
    generates nothing, so the Uzbek row never appears in the background and every Uzbek
    reader waits for it in the foreground instead. Translations had precisely this defect.
    """
    from api.routes import quiz

    warmed = []

    async def fake_warm(cluster_id, lang):
        warmed.append((cluster_id, lang))

    monkeypatch.setattr(quiz.explanations, "warm", fake_warm)

    # Uzbek, and entitled — `registered` deliberately ends the trial, and warming is gated
    # on entitlement so an unpaid user warms nothing at all.
    async with api_db() as s:
        await s.execute(sa_update(User).where(User.chat_id == 42).values(
            lang=LANG_UZ,
            pass_expires_at=datetime.now(timezone.utc) + timedelta(days=30)))
        await s.commit()

    r = await client.get("/users/42/next-question")
    assert r.status_code == 200, r.text

    assert warmed, "serving a question warmed nothing at all"
    assert warmed[0][1] == LANG_UZ, \
        f"warmed {warmed[0][1]!r} for an Uzbek reader — the Uzbek row will never be built"


# --- the script -------------------------------------------------------------

def test_the_prompt_forbids_cyrillic_uzbek():
    """Modern Uzbek is Latin. A model asked for "uzbek" with no further instruction will
    cheerfully answer in the Soviet-era Cyrillic orthography, which reads as wrong to the
    audience — the same trap the translation prompt already pins."""
    assert "LATINO" in explanations.SYSTEM_PROMPT
    assert "йўл белгиси" in explanations.SYSTEM_PROMPT, "show the model what NOT to write"


def test_the_prompt_prefers_a_missing_language_to_a_wrong_one():
    """The whole reason Uzbek was withheld was that a bad explanation is unreviewable by
    the reader. Letting the model decline one language keeps that protection while still
    writing Uzbek wherever it can."""
    assert 'ometti la chiave "uz"' in explanations.SYSTEM_PROMPT
