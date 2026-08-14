"""Tap an Italian word in a question, keep it, and find out what it means.

WHAT THE MEASUREMENT SAID, AND WHY IT DECIDED THE DESIGN

The curated glossary holds 1,104 words and covers only 14.5% of the word tokens in the
question bank. The words a learner is likeliest to tap are exactly the ones missing from it:
`raffigurato` occurs 2,796 times and is absent, as are `veicolo`, `veicoli` and `velocità`.
So a glossary lookup answers almost nothing.

But the bank holds only 5,239 distinct words. SHARED AND CACHED, the first learner to tap a
word pays for one small translation and every learner afterwards gets it free — the ceiling
is the bank translated once, ever, rather than a cost that grows with the user base. Almost
every test in this file exists to protect that property, because losing it turns a bounded
one-off into a per-tap bill nobody would notice until the invoice arrived.

THE CACHE IS NOT THE GLOSSARY. `vocab_terms` with a NULL owner is curated and
frequency-ranked and the drill walks it in teaching order; tapped words go in their own
table, and what reaches a learner's list is a PERSONAL row.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from api.models import VocabTerm, WordGloss
from api.services import wordlookup
from api.services.telegram_auth import sign
from shared.config import settings

CHAT = 42
TOKEN = "8918020834:AAEtest-token-not-real-only-for-tests"
NOW = datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setattr(settings, "bot_token_prod", TOKEN)
    monkeypatch.setattr(settings, "env", "prod")


@pytest.fixture
def model(monkeypatch):
    """A stand-in dictionary that COUNTS its calls.

    The count is the point of the fixture. Every economic claim this feature rests on is a
    claim about how often the model is reached, and a test that only checked the returned
    text would pass just as well if every tap called it.
    """
    calls: list[str] = []

    async def fake(word: str):
        calls.append(word)
        lemma = {"veicoli": "veicolo", "raffigurato": "raffigurare"}.get(word, word)
        return {"lemma": lemma, "en": f"EN {lemma}", "ru": f"RU {lemma}",
                "uz": f"UZ {lemma}"}

    monkeypatch.setattr(wordlookup, "translate", fake)
    return calls


@pytest.fixture
def premium(api_db):
    """Saving words is Premium, like the rest of the vocabulary trainer."""
    async def grant():
        from api.models import User
        async with api_db() as s:
            user = await s.get(User, CHAT)
            user.pass_expires_at = NOW + timedelta(days=30)
            await s.commit()
    return grant


def auth(chat_id: int = CHAT) -> dict:
    return {"X-Telegram-Init-Data": sign(
        {"user": json.dumps({"id": chat_id, "first_name": "Zee"}, separators=(",", ":")),
         "auth_date": str(int(time.time()))}, TOKEN)}


async def tap(client, word: str, chat_id: int = CHAT):
    return await client.post("/webapp/vocab/lookup", headers=auth(chat_id),
                             json={"word": word})


async def my_words(api_db, chat_id: int = CHAT) -> list[VocabTerm]:
    async with api_db() as s:
        return list(await s.scalars(
            select(VocabTerm).where(VocabTerm.owner_chat_id == chat_id)))


# --- what a tap does ----------------------------------------------------------

async def test_a_tapped_word_is_saved_with_its_meaning(client, registered, api_db,
                                                        model, premium):
    await premium()
    r = await tap(client, "sosta")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["it"] == "sosta"
    assert body["gloss"] == "RU sosta", "the gloss came back in the wrong language"

    words = await my_words(api_db)
    assert [w.it for w in words] == ["sosta"]
    assert (words[0].en, words[0].ru, words[0].uz) == ("EN sosta", "RU sosta", "UZ sosta"), \
        "a tapped word must carry a REAL gloss per language, not one string in all three"


async def test_the_dictionary_form_is_saved_not_what_was_tapped(client, registered,
                                                                 api_db, model, premium):
    """`veicolo` and `veicoli` are two tokens and one word. Saving the surface form fills a
    learner's list with duplicates of the same noun and doubles the shared cache."""
    await premium()
    body = (await tap(client, "veicoli")).json()
    assert body["it"] == "veicolo"
    assert [w.it for w in await my_words(api_db)] == ["veicolo"]


async def test_tapping_two_forms_of_one_word_saves_it_once(client, registered, api_db,
                                                            model, premium):
    await premium()
    await tap(client, "veicolo")
    await tap(client, "veicoli")
    assert [w.it for w in await my_words(api_db)] == ["veicolo"]


async def test_tapping_a_word_twice_is_not_an_error(client, registered, api_db,
                                                     model, premium):
    """They tapped it to find out what it means. An error is a worse answer to that than
    the meaning they already had."""
    await premium()
    first = await tap(client, "sosta")
    second = await tap(client, "sosta")
    assert second.status_code == 201
    assert second.json()["id"] == first.json()["id"]


# --- the economics, which is the whole design ---------------------------------

async def test_the_second_learner_to_tap_a_word_costs_nothing(client, registered,
                                                               api_db, model, premium):
    """THE test.

    The ceiling on this feature is "the bank translated once, ever" rather than a bill that
    grows with users, and that holds only while the cache is genuinely shared. Asserted on
    the CALL COUNT: a version that cached per-learner returns identical text and would pass
    every other test in this file.
    """
    from api.models import User

    await premium()
    async with api_db() as s:
        s.add(User(chat_id=99, lang="en", pass_expires_at=NOW + timedelta(days=30)))
        await s.commit()

    await tap(client, "raffigurato")
    assert len(model) == 1

    r = await tap(client, "raffigurato", chat_id=99)
    assert r.status_code == 201
    assert len(model) == 1, f"the second learner paid for the same word again: {model}"
    assert r.json()["gloss"] == "EN raffigurare", "the cached gloss was not served"


async def test_the_same_learner_tapping_again_costs_nothing(client, registered,
                                                             model, premium):
    await premium()
    await tap(client, "sosta")
    await tap(client, "sosta")
    assert len(model) == 1


async def test_the_cache_is_keyed_on_the_lemma_not_the_tap(client, registered, api_db,
                                                            model, premium):
    await premium()
    await tap(client, "veicoli")
    async with api_db() as s:
        rows = list(await s.scalars(select(WordGloss)))
    assert [r.lemma for r in rows] == ["veicolo"]
    from api.models import WordForm

    async with api_db() as s:
        index = await s.get(WordForm, "veicoli")
    assert index is not None and index.lemma == "veicolo", \
        "the tapped form was not indexed, so the next learner to tap it pays again"


async def test_the_cache_is_not_the_curated_glossary(client, registered, api_db,
                                                      model, premium):
    """The shared sheet is frequency-ranked and the drill walks it in teaching order.
    Writing tapped words into it would destroy both the curation and the ordering."""
    await premium()
    before = len(await my_words(api_db))
    await tap(client, "raffigurato")

    async with api_db() as s:
        shared = await s.scalar(
            select(func.count()).select_from(VocabTerm)
            .where(VocabTerm.owner_chat_id.is_(None), VocabTerm.it == "raffigurare"))
    assert shared == 0, "a tapped word was written into the shared glossary"
    assert len(await my_words(api_db)) == before + 1


# --- what is not a word --------------------------------------------------------

@pytest.mark.parametrize("raw", ["", "   ", "123", "!!!", "'", "-", "…", "42a"])
def test_things_that_are_not_words_are_refused(raw):
    assert wordlookup.normalise(raw) is None


@pytest.mark.parametrize("raw,expected", [
    ("Sosta.", "sosta"), ("«velocità»", "velocità"), ("dell'auto", "dell'auto"),
    ("VEICOLO", "veicolo"), ("  strada,  ", "strada"), ("auto-treno", "auto-treno"),
])
def test_punctuation_is_trimmed_but_the_word_survives(raw, expected):
    """Trimmed from the ENDS only. `dell'auto` is one word and must not become `dell`."""
    assert wordlookup.normalise(raw) == expected


async def test_a_non_word_is_refused_by_the_endpoint(client, registered, model, premium):
    await premium()
    r = await tap(client, "!!!")
    assert r.status_code == 422
    assert not model, "a non-word reached the model"


# --- limits and refusals --------------------------------------------------------

async def test_only_a_cache_miss_is_charged_against_the_daily_limit(client, registered,
                                                                     model, premium,
                                                                     monkeypatch):
    """The limit exists to bound what one account can spend on a COLD cache. Charging cache
    hits too would cut somebody off from words that cost nothing to serve."""
    await premium()
    monkeypatch.setattr(wordlookup, "DAILY_LOOKUPS", 2)
    assert (await tap(client, "sosta")).status_code == 201
    assert (await tap(client, "strada")).status_code == 201
    # A third NEW word is refused...
    assert (await tap(client, "velocità")).status_code == 429
    # ...but one already in the cache is still served.
    assert (await tap(client, "sosta")).status_code == 201


async def test_the_limit_is_reported_as_429_not_as_a_failure(client, registered,
                                                              model, premium, monkeypatch):
    """So the client can say "that is enough new words for today" rather than showing an
    error, which reads as the feature being broken."""
    await premium()
    monkeypatch.setattr(wordlookup, "DAILY_LOOKUPS", 0)
    assert (await tap(client, "sosta")).status_code == 429


async def test_a_free_learner_is_told_it_is_premium(client, registered, model):
    """Vocabulary is Premium, and this is the vocabulary trainer. A tap is a good moment to
    say so — they are holding a word they wanted."""
    r = await tap(client, "sosta")
    # 402, the paywall status this app already uses everywhere — not 403, which is what I
    # assumed. A new endpoint inventing its own status is a client with two paywall branches.
    assert r.status_code == 402
    assert not model, "a free learner's tap reached the model"


async def test_a_model_failure_does_not_take_the_question_down(client, registered,
                                                                premium, monkeypatch):
    """A word this fails on is a word the learner does not get. It must not be a word that
    breaks the question they were reading."""
    async def broken(_word):
        return None

    await premium()
    monkeypatch.setattr(wordlookup, "translate", broken)
    r = await tap(client, "sosta")
    assert r.status_code == 503


async def test_an_incomplete_translation_is_not_cached(client, registered, api_db,
                                                        premium, monkeypatch):
    """Half a translation cached is half a translation served for ever, to everybody."""
    async def partial(_word):
        return None            # `translate` already refuses to return a partial result

    await premium()
    monkeypatch.setattr(wordlookup, "translate", partial)
    await tap(client, "sosta")
    async with api_db() as s:
        assert await s.scalar(select(func.count()).select_from(WordGloss)) == 0


async def test_the_endpoint_needs_a_signature(client, registered):
    assert (await client.post("/webapp/vocab/lookup",
                              json={"word": "sosta"})).status_code == 401


# --- undo ----------------------------------------------------------------------

async def test_undo_removes_it_through_the_existing_delete(client, registered, api_db,
                                                            model, premium):
    """One way to delete one of these rows, not two. The client undoes a mis-tap through
    exactly the path a learner's own words already use."""
    await premium()
    body = (await tap(client, "sosta")).json()
    r = await client.delete(f"/webapp/vocab/terms/{body['id']}", headers=auth())
    assert r.status_code == 204
    assert await my_words(api_db) == []


async def test_undoing_does_not_empty_the_shared_cache(client, registered, api_db,
                                                        model, premium):
    """One learner changing their mind must not make the next learner pay for the word
    again. The cache is the product's, the saved word is theirs."""
    await premium()
    body = (await tap(client, "sosta")).json()
    await client.delete(f"/webapp/vocab/terms/{body['id']}", headers=auth())
    async with api_db() as s:
        assert await s.scalar(select(func.count()).select_from(WordGloss)) == 1
    await tap(client, "sosta")
    assert len(model) == 1, "the word was translated twice after an undo"


# --- the model call itself ------------------------------------------------------
#
# Everything above stubs `translate`, which is right for testing what the caller does with
# a result — and leaves the function's own guards untested. A mutant that deleted the
# completeness check survived every test in this file until these were written.

class _Choice:
    def __init__(self, content):
        self.message = type("M", (), {"content": content})()


class _Response:
    def __init__(self, content):
        self.choices = [_Choice(content)]


def _client_returning(payload: str, calls: list | None = None):
    """A stand-in AsyncOpenAI whose completion returns exactly this body."""
    class Completions:
        async def create(self, **kwargs):
            if calls is not None:
                calls.append(kwargs)
            return _Response(payload)

    class Chat:
        completions = Completions()

    class Client:
        def __init__(self, **_kw):
            self.chat = Chat()

    return Client


@pytest.fixture
def openai_key(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "sk-test-not-real")
    monkeypatch.setattr(settings, "openai_translate_model", "test-model")


async def test_a_complete_answer_is_accepted(monkeypatch, openai_key):
    import openai

    monkeypatch.setattr(openai, "AsyncOpenAI", _client_returning(
        '{"lemma": "veicolo", "en": "vehicle", "ru": "транспорт", "uz": "avtomobil"}'))
    assert await wordlookup.translate("veicoli") == {
        "lemma": "veicolo", "en": "vehicle", "ru": "транспорт", "uz": "avtomobil"}


@pytest.mark.parametrize("body", [
    '{"lemma": "veicolo", "en": "vehicle", "ru": "", "uz": "avtomobil"}',
    '{"lemma": "veicolo", "en": "vehicle", "uz": "avtomobil"}',
    '{"lemma": "veicolo"}',
])
async def test_a_half_translation_is_refused_rather_than_cached(monkeypatch, openai_key,
                                                                 body):
    """Half a translation cached is half a translation served for ever, to everybody. A
    learner whose interface is Russian would get a blank where the meaning should be, and
    nothing would ever try that word again."""
    import openai

    monkeypatch.setattr(openai, "AsyncOpenAI", _client_returning(body))
    assert await wordlookup.translate("veicoli") is None


async def test_a_broken_answer_is_refused_not_raised(monkeypatch, openai_key):
    """A word the model mangles must not take the question the learner was reading down
    with it."""
    import openai

    monkeypatch.setattr(openai, "AsyncOpenAI", _client_returning("not json at all"))
    assert await wordlookup.translate("veicoli") is None


async def test_the_returned_lemma_is_normalised(monkeypatch, openai_key):
    """The model is asked for a lowercase dictionary form and will sometimes send
    "Veicolo." anyway. Cached unnormalised, that is a second row for a word already in the
    cache — and a learner's list showing "Veicolo." with a full stop."""
    import openai

    monkeypatch.setattr(openai, "AsyncOpenAI", _client_returning(
        '{"lemma": "Veicolo.", "en": "vehicle", "ru": "транспорт", "uz": "avtomobil"}'))
    assert (await wordlookup.translate("veicoli"))["lemma"] == "veicolo"


async def test_the_cheap_reasoning_setting_is_actually_sent(monkeypatch, openai_key):
    """translations.py records a case where this parameter was silently dropped by a retry
    and nobody noticed for weeks, at five to ten times the cost, with the constant set and
    the tests passing. Asserted on the FIRST attempt's kwargs."""
    import openai

    calls: list = []
    monkeypatch.setattr(openai, "AsyncOpenAI", _client_returning(
        '{"lemma": "veicolo", "en": "v", "ru": "т", "uz": "a"}', calls))
    await wordlookup.translate("veicoli")
    assert calls[0].get("reasoning_effort") == wordlookup.REASONING_EFFORT
    assert calls[0].get("temperature") == 0


async def test_no_api_key_means_no_call_and_no_crash(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "")
    assert await wordlookup.translate("veicoli") is None


# --- the client ------------------------------------------------------------------

from pathlib import Path                                              # noqa: E402

MAIN = (Path(__file__).resolve().parent.parent / "webapp/src/main.ts").read_text()
CSS = (Path(__file__).resolve().parent.parent / "webapp/src/style.css").read_text()


def block_of(name: str) -> str:
    start = MAIN.index(f"function {name}(")
    end = len(MAIN)
    for marker in ("\nfunction ", "\nasync function "):
        at = MAIN.find(marker, start + 10)
        if at != -1:
            end = min(end, at)
    return MAIN[start:end]


def test_the_statement_is_rendered_word_by_word():
    """A plain <p> has nothing to attach a tap to."""
    assert "tappableStatement(question.statement_it)" in MAIN
    assert 'el("p", "statement", question.statement_it)' not in MAIN


def test_punctuation_stays_outside_the_tap_target():
    """Otherwise the target for the last word of a sentence includes the full stop, and a
    learner aiming at the word hits the gap after it."""
    body = block_of("tappableStatement")
    assert "lead" in body and "tail" in body
    assert 'el("span", "word", core)' in body


def test_it_is_a_tap_and_not_a_long_press():
    """In a Telegram WebView a long press raises native selection and the copy callout;
    suppressing that needs `user-select: none`, which then stops a learner selecting the
    Italian at all — something people genuinely do."""
    body = block_of("tappableStatement")
    assert "onclick" in body
    for gone in ("touchstart", "setTimeout", "longpress", "contextmenu"):
        assert gone not in body, f"a long-press mechanism crept in: {gone}"
    assert "user-select: none" not in CSS.split(".statement .word")[-1][:400]


def test_the_toast_carries_the_meaning_not_just_a_confirmation():
    """At the moment somebody is stuck on a word, what they want is what it means. A toast
    that only confirms a filing action gives them nothing until they open another screen."""
    body = block_of("lookUpWord")
    assert "${found.it} — ${found.gloss}" in body


def test_undo_uses_the_existing_delete():
    """One way to remove one of these rows, not two."""
    assert "vocab.removeTerm(found.id)" in block_of("lookUpWord")


def test_every_refusal_has_its_own_message():
    """A learner who has hit the daily limit, one who needs Premium, and one whose word the
    model could not translate are three different situations. One generic error for all
    three reads as the feature being broken in all three."""
    body = block_of("lookUpWord")
    for status, key in ((429, "lookup_enough_today"), (402, "lookup_premium"),
                        (503, "lookup_unavailable")):
        assert f"err.status === {status}" in body, f"{status} is not handled"
        assert key in body


def test_a_saved_word_stays_marked():
    """So a learner can see which words they have already collected without opening
    another screen."""
    assert 'node.classList.add("saved")' in block_of("lookUpWord")
    assert ".statement .word.saved" in CSS


def test_a_double_tap_does_not_send_two_lookups():
    body = block_of("lookUpWord")
    assert 'node.classList.contains("busy")' in body
