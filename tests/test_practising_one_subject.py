"""Practising one subject instead of the whole bank.

THE FAILURE THIS FEATURE IS MOST LIKELY TO HAVE is not an error — it is a sitting that
quietly contains the wrong questions. A filter applied to only one of the three tiers
`practice_paper` draws from serves the learner's overdue signs questions and then pads the
paper out with rules, and everything about that looks like it worked: the request succeeds,
the sitting starts, thirty questions appear. So the tests that matter here assert on the
CONTENTS of the paper, tier by tier.

TWO RULES ARE STRUCTURAL AND BOTH ARE REFUSALS:

  · the exam is never scoped. A simulator quietly weighted toward chosen topics reports a
    score that means nothing, which is the same argument `selection.exam_paper` already
    makes for its uniform draw.
  · an unrecognised scope is refused, not ignored. Silently widening back to the whole bank
    would look to the learner like the subject they picked being wrong, rather than like
    an error anybody could act on.

AND ONE PROPERTY IS EASY TO LOSE BY ACCIDENT: scoping must not turn practice back into a
random draw. Spaced repetition still applies inside the subject — due first.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select as sa_select

from api.models import Progress, Question, Quesito, Topic, User
from api.services import categories, selection
from api.services.entitlement import evaluate
from api.services.telegram_auth import sign
from shared.config import settings
from shared.constants import TOPIC_FAMILIES

CHAT = 42
TOKEN = "8918020834:AAEtest-token-not-real-only-for-tests"
NOW = datetime(2026, 8, 12, 10, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setattr(settings, "bot_token_prod", TOKEN)
    monkeypatch.setattr(settings, "env", "prod")


def auth(chat_id: int = CHAT) -> dict:
    return {"X-Telegram-Init-Data": sign(
        {"user": json.dumps({"id": chat_id, "first_name": "Zee"}, separators=(",", ":")),
         "auth_date": str(int(time.time()))}, TOKEN)}


# Two real families, and two topics from each, so "scoped" and "unscoped" are genuinely
# distinguishable rather than the whole fixture being one subject.
SIGNS = TOPIC_FAMILIES["signs_vertical"][0]
SIGNS_2 = TOPIC_FAMILIES["signs_vertical"][1]
RULES = TOPIC_FAMILIES["rules"][0]


async def bank(api_db, per_topic: int = 12) -> None:
    """A bank spread across two families, so a filter has something to filter."""
    async with api_db() as s:
        # Topics first and flushed on their own: `quesiti.topic_id` is a real foreign key
        # and foreign_keys=ON is set per connection, so an unflushed parent is a constraint
        # error rather than an orphan.
        for topic_id in (SIGNS, SIGNS_2, RULES):
            if await s.get(Topic, topic_id) is None:
                s.add(Topic(id=topic_id, name=f"Topic {topic_id}"))
        await s.flush()
        for topic_id in (SIGNS, SIGNS_2, RULES):
            s.add(Quesito(id=1000 + topic_id, topic_id=topic_id, primary_image=None))
        await s.flush()
        qid = 5000
        for topic_id in (SIGNS, SIGNS_2, RULES):
            for _ in range(per_topic):
                qid += 1
                s.add(Question(id=qid, quesito_id=1000 + topic_id, topic_id=topic_id,
                               statement_it=f"q{qid}", answer=True, source_version="v1"))
        await s.commit()


async def paper(api_db, scope: str | None, count: int = 10) -> list[Question]:
    async with api_db() as s:
        user = await s.get(User, CHAT)
        return await selection.practice_paper(
            s, user, evaluate(user), count, now=NOW,
            topic_ids=categories.topic_ids(scope))


async def topics_of(api_db, questions: list[Question]) -> set[int]:
    return {q.topic_id for q in questions}


# --- the draw ----------------------------------------------------------------

async def test_a_family_draws_only_from_that_family(api_db, registered):
    await bank(api_db)
    picked = await paper(api_db, "signs_vertical")
    assert picked, "the scoped draw returned nothing"
    assert await topics_of(api_db, picked) <= set(TOPIC_FAMILIES["signs_vertical"])


async def test_one_topic_draws_only_from_that_topic(api_db, registered):
    await bank(api_db)
    picked = await paper(api_db, f"topic:{SIGNS}")
    assert picked
    assert await topics_of(api_db, picked) == {SIGNS}


async def test_no_scope_still_draws_from_everything(api_db, registered):
    """The existing behaviour, pinned. Every sitting before this feature had no scope, and
    a filter that leaked into the unscoped path would narrow them all."""
    await bank(api_db, per_topic=40)
    picked = await paper(api_db, None, count=60)
    assert len(await topics_of(api_db, picked)) > 1, \
        "an unscoped paper came from a single topic"


async def test_the_filter_reaches_every_tier_not_just_the_first(api_db, registered):
    """THE test.

    `practice_paper` draws in three tiers — due, then unseen, then not-yet-due. A filter on
    the first tier alone serves the learner's overdue signs questions and then pads the
    paper out with rules, and nothing about that looks broken: the request succeeds and
    thirty questions appear.

    Constructed so all three tiers must contribute: two questions overdue, most unseen, and
    some seen but not yet due — with rules questions available in every one of those states,
    ready to leak in.
    """
    await bank(api_db, per_topic=12)
    async with api_db() as s:
        signs = list(await s.scalars(
            sa_select(Question).where(Question.topic_id == SIGNS)))
        rules = list(await s.scalars(
            sa_select(Question).where(Question.topic_id == RULES)))
        # Overdue in both families.
        for row in (signs[0], rules[0]):
            s.add(Progress(chat_id=CHAT, question_id=row.id, box=1,
                           due_at=NOW - timedelta(days=1), seen=1, wrong=1))
        # Seen but not due, in both families.
        for row in (signs[1], rules[1]):
            s.add(Progress(chat_id=CHAT, question_id=row.id, box=4,
                           due_at=NOW + timedelta(days=9), seen=3, wrong=0))
        await s.commit()

    picked = await paper(api_db, "signs_vertical", count=12)
    assert len(picked) >= 10, f"the scoped paper was short: {len(picked)}"
    assert await topics_of(api_db, picked) <= set(TOPIC_FAMILIES["signs_vertical"]), \
        "a question from outside the chosen subject reached the paper"


async def test_spaced_repetition_still_applies_inside_the_subject(api_db, registered):
    """Scoping narrows WHAT is drawn, never HOW. A scoped sitting that ignored the Leitner
    schedule would be a random draw wearing a subject's name, and the whole reason practice
    exists is that it serves what you are about to forget."""
    await bank(api_db)
    async with api_db() as s:
        signs = list(await s.scalars(
            sa_select(Question).where(Question.topic_id == SIGNS)))
        overdue = signs[3]
        s.add(Progress(chat_id=CHAT, question_id=overdue.id, box=1,
                       due_at=NOW - timedelta(days=5), seen=2, wrong=2))
        await s.commit()

    picked = await paper(api_db, "signs_vertical", count=5)
    assert picked[0].id == overdue.id, \
        "the overdue question was not served first inside the chosen subject"


# --- what a scope may be ------------------------------------------------------

def test_every_family_is_a_valid_scope():
    for family in TOPIC_FAMILIES:
        assert categories.is_valid(family)
        assert categories.topic_ids(family) == set(TOPIC_FAMILIES[family])


def test_no_scope_is_not_an_empty_filter():
    """None means "the whole bank"; an empty set would mean "no questions at all". Conflating
    them is a sitting that silently returns nothing."""
    assert categories.topic_ids(None) is None
    assert categories.topic_ids("") is None


@pytest.mark.parametrize("bad", ["nonsense", "topic:", "topic:abc", "topic:1;DROP",
                                 "signs_vertical ", "TOPIC:5"])
def test_an_unknown_scope_is_refused(bad):
    assert not categories.is_valid(bad)
    with pytest.raises(categories.UnknownScope):
        categories.topic_ids(bad)


# --- through the API ----------------------------------------------------------

async def test_starting_a_scoped_practice_sitting(client, registered, api_db):
    await bank(api_db)
    r = await client.post("/webapp/sessions", headers=auth(),
                          json={"mode": "practice", "scope": "signs_vertical"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["questions"], "the sitting came back empty"
    assert all(q["topic_id"] in TOPIC_FAMILIES["signs_vertical"] for q in body["questions"])


async def test_the_sitting_remembers_what_it_was_drawn_from(client, registered, api_db):
    """Stored rather than inferred from the questions: a paper of thirty signs questions
    could be a chosen subject or a coincidence, and the results screen has to be able to
    tell the learner which."""
    from api.models import QuizSession

    await bank(api_db)
    body = (await client.post("/webapp/sessions", headers=auth(),
                              json={"mode": "practice", "scope": "signs_vertical"})).json()
    async with api_db() as s:
        assert (await s.get(QuizSession, body["id"])).scope == "signs_vertical"


async def test_an_unscoped_sitting_records_no_scope(client, registered, api_db):
    from api.models import QuizSession

    await bank(api_db)
    body = (await client.post("/webapp/sessions", headers=auth(),
                              json={"mode": "practice"})).json()
    async with api_db() as s:
        assert (await s.get(QuizSession, body["id"])).scope is None


async def test_an_exam_refuses_a_scope(client, registered, api_db):
    """Not ignored — refused. A simulator quietly weighted toward chosen topics reports a
    score that means nothing, and silently dropping the parameter would let a client believe
    it had asked for something it did not get."""
    await bank(api_db, per_topic=40)
    r = await client.post("/webapp/sessions", headers=auth(),
                          json={"mode": "exam", "scope": "signs_vertical"})
    assert r.status_code == 422, r.text


async def test_an_unknown_scope_is_refused_by_the_endpoint(client, registered, api_db):
    await bank(api_db)
    r = await client.post("/webapp/sessions", headers=auth(),
                          json={"mode": "practice", "scope": "nonsense"})
    assert r.status_code == 422


# --- the catalogue ------------------------------------------------------------

async def test_the_catalogue_lists_every_family_with_its_topics(client, registered):
    body = (await client.get("/webapp/categories", headers=auth())).json()
    assert {c["family"] for c in body} == set(TOPIC_FAMILIES)
    for card in body:
        assert card["scope"] == card["family"]
        assert len(card["topics"]) == len(TOPIC_FAMILIES[card["family"]])
        assert all(t["scope"].startswith("topic:") for t in card["topics"])


async def test_every_scope_the_catalogue_offers_actually_works(client, registered):
    """The list and the draw agreeing is the whole contract. A catalogue offering a scope
    the endpoint then refuses is a screen full of buttons that fail."""
    body = (await client.get("/webapp/categories", headers=auth())).json()
    for card in body:
        assert categories.is_valid(card["scope"])
        for topic in card["topics"]:
            assert categories.is_valid(topic["scope"])


async def test_the_catalogue_is_ranked_by_marks_lost_not_by_size(client, registered,
                                                                 api_db):
    """Ordered by size, road signs would sit at the top for everybody forever — a table of
    contents rather than advice. The order comes from `analysis.families`, which is the same
    call and the same order the error-analysis screen uses, so the two cannot disagree."""
    from api.services import analysis

    async with api_db() as s:
        ranked = [f["family"] for f in await analysis.families(s, CHAT)]
    body = [c["family"] for c in (await client.get("/webapp/categories",
                                                   headers=auth())).json()]
    assert body == ranked


async def test_an_untested_family_reports_no_rate_rather_than_zero(client, registered):
    """On day one every one of these is untested, and a row of 0% would read as mastery of
    a bank the learner has never opened."""
    body = (await client.get("/webapp/categories", headers=auth())).json()
    assert all(c["error_rate"] is None for c in body)


async def test_the_catalogue_needs_a_signature(client, registered):
    assert (await client.get("/webapp/categories")).status_code == 401


async def test_the_filter_reaches_the_not_yet_due_tier(api_db, registered):
    """The third tier, which the test above never reaches.

    `practice_paper` fills from due, then unseen, then — only if still short — from questions
    the learner has seen but that are not due yet. Those first two tiers satisfied a
    twelve-question paper on their own, so a filter missing from the third one survived a
    test that looked like it covered all three.

    Here EVERY question in the bank has been seen, so the unseen tier is empty by
    construction and the paper cannot be filled without the third. Rules questions sit
    ready in exactly that state, waiting to leak in.
    """
    await bank(api_db, per_topic=12)
    async with api_db() as s:
        every = list(await s.scalars(sa_select(Question)))
        for i, row in enumerate(every):
            # Two overdue per family, everything else seen and comfortably in the future.
            due = NOW - timedelta(days=1) if i % 12 < 2 else NOW + timedelta(days=30)
            s.add(Progress(chat_id=CHAT, question_id=row.id, box=3, due_at=due,
                           seen=2, wrong=1))
        await s.commit()

    picked = await paper(api_db, "signs_vertical", count=12)
    assert len(picked) >= 10, f"the third tier did not fill the paper: {len(picked)}"
    assert await topics_of(api_db, picked) <= set(TOPIC_FAMILIES["signs_vertical"]), \
        "a not-yet-due question from outside the chosen subject reached the paper"


# --- the client -------------------------------------------------------------
#
# Asserted against the source, because the failure this feature is most likely to have on
# the client is the same one it has on the server: something that looks like it worked. A
# button wired to the wrong scope, or a screen that never calls the endpoint, produces a
# perfectly ordinary practice sitting.

from pathlib import Path                                              # noqa: E402

MAIN = (Path(__file__).resolve().parent.parent / "webapp/src/main.ts").read_text()
I18N = (Path(__file__).resolve().parent.parent / "webapp/src/i18n.ts").read_text()


def block_of(name: str) -> str:
    start = MAIN.index(f"function {name}(")
    end = len(MAIN)
    for marker in ("\nfunction ", "\nasync function "):
        at = MAIN.find(marker, start + 10)
        if at != -1:
            end = min(end, at)
    return MAIN[start:end]


def test_the_screen_asks_the_server_for_the_list():
    """A screen that renders a hardcoded list of seven families would look identical and
    would never show the learner their own numbers."""
    assert "categories.list()" in block_of("loadSubjects")
    assert "loadSubjects()" in block_of("practiceScreen")


def test_starting_from_a_family_passes_that_family_as_the_scope():
    body = block_of("subjectsScreen")
    assert 'startRun("practice", "smart", cat.scope)' in body


def test_starting_from_a_book_chapter_passes_the_topic_scope():
    """The two levels must send DIFFERENT scopes. Both wired to `cat.scope` would make every
    chapter start a whole-family sitting, which is the feature silently not working."""
    body = block_of("subjectsScreen")
    assert 'startRun("practice", "smart", topic.scope)' in body


def test_the_analysis_screen_can_practise_what_it_just_diagnosed():
    """The payoff. That screen ranks seven subjects by the marks each is costing and, until
    now, offered nothing to do about any of them."""
    body = block_of("analysisScreen")
    assert 'startRun("practice", "smart", f.family)' in body


def test_an_exam_is_never_started_with_a_scope():
    """Enforced on the server too, and asserted here because a client that offers it puts a
    button in front of somebody that can only ever fail."""
    import re

    for call in re.findall(r'startRun\((.*?)\)', MAIN):
        if call.strip().startswith('"exam"'):
            assert call.count(",") <= 1, f"an exam was started with a scope: {call}"


def test_the_untested_state_is_not_styled_as_a_bad_score():
    """"Not tested" is an absence of data. In the error colour it tells a beginner that
    every subject in the product is already going badly for them."""
    assert 'cat.error_rate === null ? " untested" : ""' in block_of("subjectsScreen")


def test_every_new_string_exists_in_all_four_languages():
    import re

    keys = ("subjects_entry", "subjects_title", "subjects_sub", "subjects_meta",
            "subjects_untested", "subjects_start", "subjects_show_topics",
            "subjects_hide_topics", "an_practise")
    for lang in ("it", "ru", "en", "uz"):
        m = re.search(rf'^  {lang}: \{{(.*?)^  \}},', I18N, re.M | re.S)
        assert m, f"no {lang} block"
        block = dict(re.findall(r'^\s+(\w+): "((?:[^"\\]|\\.)*)"', m.group(1), re.M))
        for key in keys:
            assert key in block, f"{lang} is missing {key}"
            assert block[key].strip(), f"{lang}.{key} is empty"


# --- where the four choices live --------------------------------------------
#
# They were on the HOME screen: two repeat chips and a subject row under the mode cards,
# which made five entry points for two activities and pushed the choice the screen exists
# for down the page. All four sit behind the practice card now.

def test_the_home_screen_offers_two_activities_not_five():
    body = block_of("homeScreen")
    for gone in ("repeatRow()", "subjectRow()"):
        assert gone not in body, f"{gone} is still on the home screen"
    assert "modeCard(\"exam\"" in body and "modeCard(\"practice\"" in body
    assert "vocabEntry()" in body


def test_the_practice_card_opens_the_choices_and_the_exam_card_does_not():
    """The exam has one way to sit it, so its card still starts it. Giving the exam a menu
    too would put a screen in front of the one action that should never be negotiable."""
    body = block_of("modeCard")
    assert 'startRun("exam")' in body
    assert 'state.screen = "practice"' in body


def test_all_four_practice_variants_are_on_one_screen():
    body = block_of("practiceScreen")
    assert 'startRun("practice")' in body, "the default is missing"
    assert 'startRun("practice", "wrong")' in body
    assert 'startRun("practice", "correct")' in body
    assert 'state.screen = "subjects"' in body


def test_the_default_is_the_only_primary_control():
    """It costs one extra tap compared with the card starting immediately. That is only an
    acceptable trade while the tap is obvious and lands on the biggest thing on screen."""
    body = block_of("practiceScreen")
    assert body.count("btn primary") == 1
    # ...and it comes before the three alternatives.
    assert body.index("practice-main") < body.index("practice-option")


def test_the_default_is_not_described_as_random():
    """It serves what the learner is about to forget, oldest due first. "Random" describes
    the exact thing practice was fixed for not being — it used to draw uniformly and never
    read back the Leitner schedule it was writing."""
    import re

    for lang in ("it", "ru", "en", "uz"):
        m = re.search(rf'^  {lang}: \{{(.*?)^  \}},', I18N, re.M | re.S)
        block = dict(re.findall(r'^\s+(\w+): "((?:[^"\\]|\\.)*)"', m.group(1), re.M))
        for key in ("practice_start", "practice_start_sub"):
            assert key in block, f"{lang} is missing {key}"
        low = f"{block['practice_start']} {block['practice_start_sub']}".lower()
        for word in ("random", "случайн", "casual", "tasodif"):
            assert word not in low, f"{lang} calls the default draw {word!r}"


def test_going_back_from_subjects_lands_on_practice_not_home():
    """Subjects is nested under practice now. Skipping a level on the way back is how a
    learner loses the screen they were choosing from."""
    body = block_of("backTarget")
    at = body.index('state.screen === "subjects"')
    assert 'state.screen = "practice"' in body[at:at + 200]


def test_no_dead_styles_were_left_behind():
    """The chips and the row are gone from the markup. CSS for elements nothing renders is
    the kind of thing that survives three redesigns and confuses the fourth."""
    css = (Path(__file__).resolve().parent.parent / "webapp/src/style.css").read_text()
    for gone in (".repeat-chip", ".repeat-row", ".subject-row"):
        assert gone not in css, f"{gone} is styled but never rendered"
