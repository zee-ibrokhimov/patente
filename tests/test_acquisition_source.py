"""Where a user came from, recorded once and never again.

Added before there is any traffic, because it is the one number that cannot be
reconstructed afterwards. When a hundred learners arrive the owner will want to know
which post brought them, and by then every row that could have said so already exists
with nothing on it.

`t.me/quizpatente_bot?start=tg_uzbeks_italy` arrives as the message "/start
tg_uzbeks_italy". The payload is captured by the middleware that CREATES the user, not by
the /start handler — by the time a handler runs the row already exists.
"""

from __future__ import annotations

import pytest

from api.models import User
from api.services import users as users_service
from api.services.users import _clean_source
from bot.middlewares import _start_payload


class Event:
    def __init__(self, text=None):
        self.text = text


# --- reading the payload off a message --------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("/start tg_uzbeks_italy", "tg_uzbeks_italy"),
    ("/start  spaced_out  ", "spaced_out"),
    ("/start", None),
    ("/start plan", "plan"),
    ("/plan", None),
    ("hello", None),
    (None, None),
])
def test_only_a_start_message_carries_a_source(text, expected):
    assert _start_payload(Event(text)) == expected


def test_a_callback_has_no_text_and_does_not_explode():
    class Callback:
        pass
    assert _start_payload(Callback()) is None


# --- it is user-controlled text ---------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("tg_uzbeks_italy", "tg_uzbeks_italy"),
    ("Instagram-2026", "Instagram-2026"),
    ("drop table users;--", "droptableusers--"),
    ("<script>alert(1)</script>", "scriptalert1script"),
    ("  ", None),
    ("", None),
    (None, None),
])
def test_the_payload_is_sanitised(raw, expected):
    """It arrives from a URL and is written to the database, then read back onto an admin
    screen. Telegram limits it to [A-Za-z0-9_-] already; this does not take that on trust."""
    assert _clean_source(raw) == expected


def test_an_absurdly_long_payload_is_cut():
    assert len(_clean_source("a" * 500)) == 64


# --- recorded once ----------------------------------------------------------

async def test_the_first_link_gets_the_credit(api_db):
    """THE rule. Someone who arrives again through a different link was still acquired by
    the first one; letting the newest claim them would credit whichever channel they
    happened to revisit rather than the one that worked."""
    async with api_db() as s:
        await users_service.get_or_create(s, 900, "ru", "first_post")
        await s.commit()
    async with api_db() as s:
        await users_service.get_or_create(s, 900, "ru", "a_later_post")
        await s.commit()
    async with api_db() as s:
        assert (await s.get(User, 900)).source == "first_post"


async def test_a_user_who_predates_this_can_still_be_attributed(api_db):
    """Everyone who signed up before the column existed has NULL. If they later arrive
    through a tracked link, that is better information than nothing — and it cannot
    overwrite anything, because there is nothing there."""
    async with api_db() as s:
        await users_service.get_or_create(s, 901, "ru")
        await s.commit()
    async with api_db() as s:
        await users_service.get_or_create(s, 901, "ru", "found_them")
        await s.commit()
    async with api_db() as s:
        assert (await s.get(User, 901)).source == "found_them"


async def test_no_payload_means_no_source_not_an_empty_string(api_db):
    """"direct" is a reporting label, not a value to store — an empty string would make
    the breakdown show two different kinds of nothing."""
    async with api_db() as s:
        await users_service.get_or_create(s, 902, "ru", "")
        await s.commit()
    async with api_db() as s:
        assert (await s.get(User, 902)).source is None


# --- it reaches the owner ---------------------------------------------------

async def test_the_overview_breaks_users_down_by_source(api_db):
    """A count with no breakdown cannot tell the owner which channel to post in again."""
    from api.services import admin

    async with api_db() as s:
        await users_service.get_or_create(s, 910, "ru", "telegram_group")
        await users_service.get_or_create(s, 911, "ru", "telegram_group")
        await users_service.get_or_create(s, 912, "ru", "instagram")
        await users_service.get_or_create(s, 913, "ru")
        await s.commit()
    async with api_db() as s:
        found = {r["source"]: r["users"] for r in (await admin.overview(s))["sources"]}
    assert found.get("telegram_group") == 2
    assert found.get("instagram") == 1
    assert found.get("direct") >= 1, "untracked arrivals must still be counted"
