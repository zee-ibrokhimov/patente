"""The two numbers on the Vocabulary screen are the same number.

The screen read:

    Словарь
    1090 экзаменационных слов        <- a literal, typed into eight locale strings
    0 из 1104 выучено                <- SELECT count(*) FROM vocab_terms

Both describe the size of one table. Only the second was ever real: the glossary was
seeded at 1090, later grew to 1104, and four translations of one sentence stayed behind.
Nothing failed, because a sentence cannot be wrong in a way a compiler notices.

The fix is not "change 1090 to 1104" — that is the same bug with a fresher date on it.
The count now comes from the database on both lines, and these tests fail if a number
describing the content is ever written into the prose again.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from sqlalchemy import func, select

from api.models import VocabTerm

WEBAPP = Path(__file__).resolve().parents[1] / "webapp" / "src"
I18N = (WEBAPP / "i18n.ts").read_text(encoding="utf-8")
MAIN = (WEBAPP / "main.ts").read_text(encoding="utf-8")

# The keys that name the size of the glossary, in every language.
SIZE_KEYS = ("v_sub", "f_vocab_s")


def values_of(key: str) -> list[str]:
    """Every translation of one key, one per language block."""
    found = re.findall(rf'^\s*{key}: "(.*)",$', I18N, re.M)
    assert found, f"{key} is no longer a locale key — this test is stale"
    return found


# --- the strings ------------------------------------------------------------

@pytest.mark.parametrize("key", SIZE_KEYS)
def test_no_locale_string_states_a_size(key: str):
    """THE bug. A digit here is a fact that cannot be kept true."""
    for value in values_of(key):
        assert not re.search(r"\d", value), (
            f'{key} hardcodes a count: "{value}". The size must be interpolated, '
            f"or it starts lying the next time the glossary is reseeded.")


@pytest.mark.parametrize("key", SIZE_KEYS)
def test_every_language_interpolates_the_count(key: str):
    """Dropping the placeholder while removing the digits would render '  exam words'."""
    for value in values_of(key):
        assert "{n}" in value, f'{key} has no placeholder to fill: "{value}"'


def test_all_four_languages_are_covered():
    """it/ru/en/uz. If a language block is missed, that language keeps the stale number
    and the tests above still pass on the three that were fixed."""
    for key in SIZE_KEYS:
        assert len(values_of(key)) == 4, f"{key}: {len(values_of(key))} languages, expected 4"


# --- and they are filled from the server, not from another literal ----------

def test_the_count_comes_from_the_user_payload():
    """`vocabSize` reads `me.vocab_terms` — the same table `v_progress` counts. A client
    constant would satisfy every test above and still drift on the next reseed."""
    match = re.search(r"function vocabSize\(\)[^}]+}", MAIN)
    assert match, "vocabSize() is gone — where does the number come from now?"
    assert "state.me?.vocab_terms" in match.group(0), (
        f"vocabSize no longer reads the server's count: {match.group(0)}")


def test_no_render_site_asks_for_the_size_without_supplying_it():
    """A `t("v_sub")` with no vars renders the literal '{n}' at the user.

    Matched to the end of the line rather than to the next ')': the argument being looked
    for is itself a call, so a non-greedy match stops inside `vocabSize(` and reports
    every correct site as broken.
    """
    for key in SIZE_KEYS:
        sites = [m for m in re.finditer(rf't\(\s*"{key}"', MAIN)]
        assert sites, f'nothing renders t("{key}") any more — this test is stale'
        for call in sites:
            line = MAIN[call.start():MAIN.find("\n", call.start())]
            assert "vocabSize()" in line, f"{key} is rendered without a count at: {line}"


# --- the server actually sends it -------------------------------------------

async def add_term(api_db, rank: int, it: str) -> None:
    async with api_db() as session:
        session.add(VocabTerm(rank=rank, it=it, ru="—", en="—", uz="—"))
        await session.commit()


async def test_me_reports_the_real_number_of_terms(api_db, client, registered):
    """Not a fixed number: whatever is in the table right now."""
    async with api_db() as session:
        expected = await session.scalar(select(func.count()).select_from(VocabTerm))

    res = await client.get(f"/users/{registered['chat_id']}")
    assert res.status_code == 200
    assert res.json()["vocab_terms"] == expected


async def test_the_number_follows_the_table(api_db, client, registered):
    """Proves it is a query and not a constant that happens to match today.

    Without this, `vocab_terms = 1104` in the route would pass every other test here —
    and would be exactly as wrong as `1090` was, just later.
    """
    url = f"/users/{registered['chat_id']}"
    before = (await client.get(url)).json()["vocab_terms"]

    await add_term(api_db, 9001, "autoveicolo")

    after = (await client.get(url)).json()["vocab_terms"]
    assert after == before + 1, (
        f"the payload did not follow the table: {before} -> {after}. "
        f"It is a constant, not a count.")
