"""The language you READ questions in, chosen separately from the language of the app.

`users.lang` did both jobs: it picked the interface strings and, through
`translations.deliver`, the language a question was translated into. So an Uzbek speaker who
reads Russian more comfortably — common enough that Uzbek shipped as beta — had to switch the
whole app to Russian to get Russian translations.

`translation_lang` is that second choice. NULL means "follow the interface language", which
is what every row created before the column existed still means and what the product did
when there was one field.

THE TRAP THIS CREATES, and most of what is checked below: FOUR places decide what language a
learner reads in, and they have to agree.

  · translations.deliver        — serves the translation
  · entitlement.translation_offer — decides whether the strip is SHOWN, LOCKED or OFF
  · explanations.deliver        — serves the prose under the answer
  · content.py                  — the bot's own rendering of a question

If the paywall reads one field and the payload reads another, a learner sees a locked strip
for a translation the API would happily have served, or an unlocked one for a language that
does not exist. They all go through `reading_language`, and there is a test here that fails
if a new call site starts reading `user.lang` directly.
"""

from __future__ import annotations

import json
import pathlib
import re
import time

import pytest
from sqlalchemy import select

from api.models import Question, Translation, User
from api.services.telegram_auth import sign
from api.services.translations import reading_language
from shared.config import settings
from shared.constants import TRANSLATION_LANGUAGES

TOKEN = "8918020834:AAEtest-token-not-real-only-for-tests"
OWNER = 42
ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setattr(settings, "bot_token_prod", TOKEN)
    monkeypatch.setattr(settings, "env", "prod")


def auth(chat_id: int = OWNER) -> dict:
    return {"X-Telegram-Init-Data": sign(
        {"user": json.dumps({"id": chat_id}, separators=(",", ":")),
         "auth_date": str(int(time.time()))}, TOKEN)}


class _User:
    """Just the two fields `reading_language` reads."""

    def __init__(self, lang, translation_lang=None):
        self.lang = lang
        self.translation_lang = translation_lang


# --- the choice itself ------------------------------------------------------

def test_null_follows_the_interface_language():
    """What every existing row means, and what the product did before the column."""
    assert reading_language(_User("ru")) == "ru"
    assert reading_language(_User("uz")) == "uz"


def test_a_chosen_language_wins_over_the_interface_one():
    """The whole point: read the app in Uzbek, read the questions in Russian."""
    assert reading_language(_User("uz", "ru")) == "ru"


# --- the four call sites have to agree --------------------------------------

def test_nothing_in_the_translation_path_reads_the_interface_language_directly():
    """A new call site that reaches for `user.lang` reintroduces the split this column
    exists to remove — and does it silently, because the two agree for every learner who
    has not changed the setting, which is all of them on the day the code is written.
    """
    import io
    import tokenize

    offenders = []
    for name in ("api/services/translations.py", "api/services/entitlement.py",
                 "api/services/content.py", "api/services/explanations.py"):
        path = ROOT / name
        # Tokenised rather than grepped. Several of these modules EXPLAIN the old field in
        # their prose — the first version of this test flagged its own docstring — and a
        # test that cannot tell code from commentary is a test that gets silenced.
        with path.open("rb") as fh:
            for tok in tokenize.tokenize(fh.readline):
                if tok.type in (tokenize.COMMENT, tokenize.STRING):
                    continue
                if tok.type != tokenize.NAME or tok.string != "lang":
                    continue
                line = tok.line.strip()
                if "user.lang" not in line:
                    continue
                # `reading_language` itself is the one place that may read it.
                if "return user.translation_lang or user.lang" in line:
                    continue
                offenders.append(f"{name}:{tok.start[0]}: {line}")
    assert not offenders, (
        "these read the interface language where they should read the reading language:\n"
        + "\n".join(offenders)
    )


# --- the API surface --------------------------------------------------------

async def test_the_setting_round_trips(client, registered, api_db):
    r = await client.patch("/webapp/settings", headers=auth(),
                          json={"translation_lang": "en"})
    assert r.status_code == 200
    assert r.json()["translation_lang"] == "en"

    async with api_db() as s:
        assert (await s.get(User, OWNER)).translation_lang == "en"


async def test_an_empty_string_goes_back_to_following_the_interface(client, registered, api_db):
    """The client needs a way to say "no separate choice", and `None` already means "leave
    this setting alone" in a PATCH-shaped body."""
    await client.patch("/webapp/settings", headers=auth(), json={"translation_lang": "en"})
    r = await client.patch("/webapp/settings", headers=auth(), json={"translation_lang": ""})
    assert r.status_code == 200
    assert r.json()["translation_lang"] is None

    async with api_db() as s:
        assert (await s.get(User, OWNER)).translation_lang is None


async def test_italian_is_refused_as_a_reading_language(client, registered):
    """Italian is a UI language and NOT a translation target — the question is already
    Italian. Accepting it would store a preference that `deliver` silently treats as OFF,
    and the learner would watch translations vanish with their choice still on screen."""
    r = await client.patch("/webapp/settings", headers=auth(), json={"translation_lang": "it"})
    assert r.status_code == 422

    r = await client.patch("/webapp/settings", headers=auth(), json={"translation_lang": "de"})
    assert r.status_code == 422


async def test_a_question_comes_back_in_the_chosen_language(client, premium_user, api_db):
    """The behaviour all of the above exists for."""
    async with api_db() as s:
        qid = (await s.scalars(select(Question.id))).first()
        s.add(Translation(question_id=qid, lang="en", statement="The sign forbids transit"))
        await s.commit()

    await client.patch("/webapp/settings", headers=auth(),
                      json={"lang": "ru", "translation_lang": "en"})
    body = (await client.post(f"/webapp/questions/{qid}/translation", headers=auth())).json()
    assert body["translation_state"] == "shown"
    assert body["translation"]["lang"] == "en"


@pytest.fixture
async def premium_user(client, registered, api_db):
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import update as sa_update
    async with api_db() as s:
        await s.execute(sa_update(User).where(User.chat_id == OWNER).values(
            pass_expires_at=datetime.now(timezone.utc) + timedelta(days=30)))
        await s.commit()
    return registered


# --- the client -------------------------------------------------------------

def test_the_control_offers_off_and_the_three_languages():
    main = (ROOT / "webapp" / "src" / "main.ts").read_text(encoding="utf-8")
    block = main[main.index("function translationToggle("):]
    block = block[:block.index("\nfunction ")]
    assert "TRANSLATION_LANGUAGES.map" in block, "the picker must list the real languages"
    assert 'tr_off' in block

    i18n = (ROOT / "webapp" / "src" / "i18n.ts").read_text(encoding="utf-8")
    for key in ("tr_off", "lang_ru", "lang_en", "lang_uz"):
        assert i18n.count(f"{key}:") == 4, f"{key} missing from a locale"


def test_the_language_names_are_endonyms():
    """A list of languages is read by someone looking for their own. "Russo" / "Русский" /
    "Rus tili" are three ways of hiding the one word they are scanning for, so the name is
    the same in every locale."""
    i18n = (ROOT / "webapp" / "src" / "i18n.ts").read_text(encoding="utf-8")
    for key, name in (("lang_ru", "Русский"), ("lang_en", "English")):
        assert i18n.count(f'{key}: "{name}",') == 4, f"{key} is not {name} in every locale"


def test_changing_the_language_reloads_the_questions_already_fetched():
    """The questions in hand carry the OLD language's text. Leaving it there shows Russian
    under a question the learner has just asked to read in English."""
    main = (ROOT / "webapp" / "src" / "main.ts").read_text(encoding="utf-8")
    block = main[main.index("function translationToggle("):]
    block = block[:block.index("\nfunction ")]
    change = block[block.index("} else {"):]
    assert "dropLoadedTranslations()" in change and "warmTranslations()" in change


def test_off_and_a_language_are_sent_as_one_call():
    """Two calls would leave a window where the server had translations_on without the
    language, or the language without the switch."""
    main = (ROOT / "webapp" / "src" / "main.ts").read_text(encoding="utf-8")
    block = main[main.index("function translationToggle("):]
    block = block[:block.index("\nfunction ")]
    assert block.count("api.settings(") == 1, "the picker makes more than one settings call"
    call = block[block.index("api.settings("):]
    call = call[:call.index("});")]
    assert "translations_on" in call and "translation_lang" in call
