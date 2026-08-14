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
    """A plain <p> has nothing to attach a press to."""
    assert "tappableStatement(question.statement_it)" in MAIN
    assert 'el("p", "statement", question.statement_it)' not in MAIN


def test_punctuation_stays_outside_the_press_target():
    """Otherwise the target for the last word of a sentence includes the full stop, and a
    learner aiming at the word holds the gap after it."""
    body = block_of("tappableStatement")
    assert "lead" in body and "tail" in body
    assert "holdableWord(core)" in body


def test_it_is_a_hold_and_not_a_tap():
    """The owner asked for a two-to-three-second hold, twice, after I argued for a tap.

    A stray tap must do nothing at all — that is the whole reason a hold was wanted, and a
    click handler left behind beside the timer would silently restore the behaviour it
    replaced.
    """
    body = block_of("holdableWord")
    assert "onpointerdown" in body and "setTimeout" in body
    assert "onclick" not in body, "a tap still saves the word"


def test_the_hold_is_between_two_and_three_seconds():
    """The number the owner asked for. Pinned so a later "just make it snappier" is a
    decision somebody takes on purpose rather than a drift."""
    import re

    m = re.search(r"const HOLD_MS = (\d+);", MAIN)
    assert m, "the hold duration is not a named constant"
    assert 2000 <= int(m.group(1)) <= 3000, f"HOLD_MS is {m.group(1)}"


def test_the_native_selection_callout_is_suppressed():
    """A long press in a WebView raises the text-selection callout, which covers the very
    word being held. This is the cost of the gesture and it has to be paid explicitly."""
    rule = CSS.split(".statement .word {")[1].split("}")[0]
    assert "-webkit-touch-callout: none" in rule
    assert "user-select: none" in rule
    assert "oncontextmenu" in block_of("holdableWord"), \
        "some WebViews raise the callout regardless of the CSS"


def test_a_drag_cancels_the_hold():
    """A press that becomes a drag is somebody scrolling the question, not choosing a word.
    Without this, scrolling adds whatever word the finger started on."""
    body = block_of("holdableWord")
    assert "onpointermove" in body and "HOLD_SLOP" in body
    assert "endHold()" in body


def test_letting_go_early_cancels_it():
    body = block_of("holdableWord")
    for event in ("onpointerup", "onpointercancel", "onpointerleave"):
        assert event in body, f"{event} does not cancel the hold"


def test_the_hold_shows_its_own_progress():
    """Two seconds of nothing happening is indistinguishable from a dead control."""
    assert ".statement .word.holding" in CSS
    assert "word-hold" in CSS, "there is no fill animation"


def test_the_fill_and_the_timer_share_one_number():
    """Two numbers drift, and a bar that fills before the save lands is a control lying
    about what it has done."""
    assert 'word.style.setProperty("--hold", `${HOLD_MS}ms`)' in block_of("holdableWord")
    assert "animation: word-hold var(--hold" in CSS


def test_reduced_motion_still_shows_the_hold_registered():
    """Somebody who has switched animation off still needs to see that their press landed.

    And the fill must be switched OFF rather than sped up. There is a global reduced-motion
    rule in this stylesheet that forces `animation-duration: .01ms !important` on everything
    — under it the bar would snap to full instantly while the timer still ran for two
    seconds, so the control would show "done" a moment after the press began and then do
    nothing. `animation: none` removes the animation itself, which `!important` on the
    duration cannot override.
    """
    # Found by CONTENT, not by proximity. The first version searched 600 characters after
    # the `.holding` rule and broke the moment a comment was added above it — a test failing
    # because prose grew, while the behaviour it guards was untouched.
    # The block's OWN body, which is the text before its closing brace — not "everything
    # after the marker", which is most of the stylesheet and contains the rule being looked
    # for anyway, so the first version matched the global reduced-motion block instead.
    blocks = [b[:b.index("\n}")] for b in CSS.split("@media (prefers-reduced-motion")[1:]]
    blocks = [b for b in blocks if ".statement .word.holding" in b]
    assert blocks, "the hold has no reduced-motion rule at all"
    rule = blocks[0]
    assert "animation: none" in rule, "the fill is sped up rather than switched off"
    assert "background:" in rule, "nothing shows that the press registered"


def test_the_learner_feels_it_land_and_feels_it_fail():
    """A gesture with no physical confirmation leaves somebody holding a word and wondering
    whether two seconds was enough.

    The FAILURE buzz matters as much: holding for two seconds and getting a toast that says
    "that is enough new words for today" is much easier to read if the phone has already
    told you it did not work.
    """
    body = block_of("lookUpWord")
    assert 'haptic("success")' in body
    assert 'haptic("error")' in body


def test_the_confirmation_is_not_styled_as_an_error():
    """`.toast` is the error toast — var(--bad) with a red shadow, because until this
    feature every message it carried was a failure. "Added to your vocabulary" in that red
    reads as something having gone wrong, which is the opposite of what just happened."""
    assert '"toast toast-action ok"' in block_of("actionToast")
    assert ".toast.ok" in CSS
    rule = CSS.split(".toast.ok {")[1].split("}")[0]
    assert "var(--bad)" not in rule


def test_the_toast_says_it_was_added_and_what_it_means():
    """The owner asked for the confirmation. The meaning is there too because somebody who
    has just held a word for two seconds is asking what it is, and sending them to another
    screen to find out wastes the moment they were curious."""
    body = block_of("lookUpWord")
    assert "${found.it} — ${found.gloss}" in body
    assert 't("word_added")' in body


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


def test_a_second_hold_does_not_send_two_lookups():
    body = block_of("lookUpWord")
    assert 'node.classList.contains("busy")' in body


def test_the_confirmation_is_not_a_pill():
    """SHIPPED WRONG AND CAUGHT ON A REAL PHONE.

    `.toast` is `border-radius: 999px`, which is right for one short line of error text and
    catastrophic for two lines plus a button: the radius eats its own corners, the text wraps
    into the curve, and the result is a four-line blob. Reading the CSS would not have shown
    it — the rule was correct for every message that existed when it was written.
    """
    rule = CSS.split(".toast-action {")[1].split("}")[0]
    assert "border-radius: var(--card-radius)" in rule, "still a pill"
    assert "999px" not in rule


def test_the_confirmation_does_not_cover_the_answer_buttons():
    """The default 84px is measured against the TAB BAR, which the run screen does not have
    — it has two full-width answer buttons in exactly that space. A toast sitting on top of
    the control somebody is about to press is worse than no toast."""
    rule = CSS.split(".toast-action {")[1].split("}")[0]
    assert "--toast-bottom" in rule, "the offset is not overridable per screen"
    run = CSS.split(".screen.run { --toast-bottom:")[1].split("}")[0]
    assert int(run.strip().rstrip("px;").strip()) >= 150, \
        "the run screen's offset does not clear two stacked buttons"


# --- alternatives ---------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    (["схема", "рисунок"], "схема, рисунок"),      # what the schema asks for
    ("схема или рисунок", "схема, рисунок"),        # a model that answered in prose anyway
    (["diagram or picture"], "diagram, picture"),   # ...in English
    (["sxema yoki rasm"], "sxema, rasm"),           # ...in Uzbek
    (["Схема", "схема"], "Схема"),                  # the same answer written twice
    (["обгон"], "обгон"),                           # a word that honestly has one meaning
    (["a, b"], "a, b"),                             # already comma-separated
    ([], ""),
    (None, ""),
])
def test_alternatives_always_come_out_comma_separated(raw, expected):
    """THE guarantee, and it is structural now rather than a request.

    The glossary stores alternatives comma-separated — "звуковой сигнал, клаксон" — and
    `vocab_grading.accepted_answers` splits on the comma, so a learner typing either is
    marked correct. Store "схема или рисунок" and the drill accepts NEITHER, because the
    whole phrase becomes one expected string.

    An earlier version asked the model for a comma-separated string and this test grepped
    the prompt for the instruction. That checked that we had ASKED, not that we had got —
    and the model, whose temperature cannot be pinned on this account, complied only
    sometimes. The model now returns a list and the join happens here, so the separator
    stops being something it can get wrong, and anything that still arrives as prose is
    split on the way past.
    """
    assert wordlookup._join(raw) == expected


def test_a_comma_separated_gloss_grades_either_answer_right():
    """The property the comma buys, checked against the real grader rather than trusted."""
    from api.services.vocab_grading import Verdict, grade

    for typed in ("схема", "рисунок"):
        assert grade(typed, "схема, рисунок", "ru").verdict is Verdict.CORRECT


def test_the_word_or_between_alternatives_would_break_the_drill():
    """The failure this convention avoids, pinned so nobody "improves" the prompt into it."""
    from api.services.vocab_grading import Verdict, grade

    for typed in ("схема", "рисунок"):
        assert grade(typed, "схема или рисунок", "ru").verdict is not Verdict.CORRECT


async def test_alternatives_survive_the_whole_path(client, registered, api_db, premium,
                                                    monkeypatch):
    """A gloss with a comma has to reach the learner's vocabulary intact — not truncated at
    the comma on the way through, which would silently discard the second meaning."""
    async def two_meanings(_word):
        # `translate` returns the JOINED string, which is what `look_up` stores — the list
        # shape lives inside `translate` and is covered by its own tests above.
        return {"lemma": "figura", "en": "diagram, picture",
                "ru": "схема, рисунок", "uz": "sxema, rasm"}

    await premium()
    monkeypatch.setattr(wordlookup, "translate", two_meanings)
    body = (await tap(client, "figura")).json()
    assert body["gloss"] == "схема, рисунок"
    words = await my_words(api_db)
    assert words[0].ru == "схема, рисунок"
    assert words[0].en == "diagram, picture"


def test_the_hold_fill_is_green_not_a_neutral_grey():
    """The owner read the old blue-grey fill as "the loading grey part", which is exactly
    what it looked like. A neutral tint says "waiting"; this is not a wait, it is something
    being earned, and green is what this app already uses for that."""
    rule = CSS.split(".statement .word.holding {")[1].split("}")[0]
    assert "--practice-chip" in rule
    assert "--tint-info" not in rule


async def test_a_list_from_the_model_reaches_the_learner_as_a_comma_string(monkeypatch,
                                                                           openai_key):
    """End to end through `translate`: lists in, one storable gloss out.

    This is the shape the schema actually asks for, so it is the shape most calls take —
    and nothing else in this file exercises it, because the tests above stub `translate`
    itself.
    """
    import openai

    monkeypatch.setattr(openai, "AsyncOpenAI", _client_returning(
        '{"lemma": "figura", "en": ["diagram", "picture"], '
        '"ru": ["схема", "рисунок"], "uz": ["sxema", "rasm"]}'))
    assert await wordlookup.translate("figure") == {
        "lemma": "figura", "en": "diagram, picture",
        "ru": "схема, рисунок", "uz": "sxema, rasm"}


async def test_an_empty_list_is_an_incomplete_answer(monkeypatch, openai_key):
    """A language the model declined to fill must not be cached as blank — the cache is
    permanent, so a blank gloss is a blank gloss served to every Uzbek learner for ever."""
    import openai

    monkeypatch.setattr(openai, "AsyncOpenAI", _client_returning(
        '{"lemma": "figura", "en": ["diagram"], "ru": ["схема"], "uz": []}'))
    assert await wordlookup.translate("figura") is None
