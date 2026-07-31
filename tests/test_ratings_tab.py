"""The Ratings tab exists, is reachable, and is translated.

A leaderboard nobody can open is a database query. These assertions are on the source
because there is no browser in this suite — narrow, but they catch the things that actually
go wrong when a screen is added: a tab that renders nothing, a case the router does not
handle, a locale left behind by an i18n sweep, and a privacy switch the user cannot find.
"""

from __future__ import annotations

import json
import pathlib
import re

import pytest

from shared.constants import LEADERBOARD_MIN_PLAYERS, UI_LANGUAGES

ROOT = pathlib.Path(__file__).resolve().parent.parent
MAIN = (ROOT / "webapp/src/main.ts").read_text(encoding="utf-8")
I18N = (ROOT / "webapp/src/i18n.ts").read_text(encoding="utf-8")
CSS = (ROOT / "webapp/src/style.css").read_text(encoding="utf-8")
API = (ROOT / "webapp/src/api.ts").read_text(encoding="utf-8")

KEYS = (
    "ratings", "ratings_week", "ratings_you", "ratings_anon", "ratings_empty",
    "ratings_quiet", "ratings_not_ranked", "ratings_hidden", "ratings_show_me",
    "ratings_visible", "ratings_visible_sub",
)


def block(lang: str) -> dict[str, str]:
    m = re.search(rf'^  {lang}: \{{(.*?)^  \}},', I18N, re.M | re.S)
    assert m, f"no {lang} block"
    return dict(re.findall(r'^\s+(\w+): "((?:[^"\\]|\\.)*)"', m.group(1), re.M))


# --- reachable ---------------------------------------------------------------

def test_the_tab_bar_offers_ratings():
    assert re.search(r'add\("ratings",\s*t\("ratings"\)', MAIN), \
        "the leaderboard has no tab, so nobody can open it"


def test_the_router_handles_it():
    """A tab that sets a screen the router does not know falls through to home, which reads
    as the tab being broken."""
    assert 'case "ratings": screen = ratingsScreen(); break;' in MAIN


def test_it_is_fetched_when_the_tab_is_opened():
    """Not at boot: it is the one screen whose data is about OTHER people, so it is stale
    the moment it is cached, and paying for it on a visit that never opens the tab is
    waste."""
    assert 'if (id === "ratings") void loadRatings();' in MAIN
    assert "board: () => request<Leaderboard>" in API


def test_the_screen_renders_a_podium_and_a_list():
    body = MAIN[MAIN.index("function ratingsScreen"):]
    body = body[:body.index("\nasync function setLeaderboardOptOut")]
    assert "podium" in body
    assert "rank-list" in body
    assert ".slice(0, 3)" in body, "no podium for the top three"
    assert ".slice(3)" in body, "places 4 and below are not listed"


def test_the_styles_exist():
    for selector in (".podium", ".podium-seat", ".rank-row", ".rank-list"):
        assert selector in CSS, f"{selector} has no styling"


# --- the small-N problem is surfaced, not hidden -----------------------------

def test_the_client_says_when_it_is_too_quiet_to_be_a_competition():
    """With four users somebody is permanently last and nobody's position can move, which
    is demoralising rather than motivating."""
    assert "RATINGS_MIN_PLAYERS" in MAIN
    assert re.search(r"board\.ranked < RATINGS_MIN_PLAYERS", MAIN)


def test_the_client_threshold_matches_the_server():
    m = re.search(r"const RATINGS_MIN_PLAYERS = (\d+);", MAIN)
    assert m, "no threshold in the client"
    assert int(m.group(1)) == LEADERBOARD_MIN_PLAYERS


# --- the privacy switch ------------------------------------------------------

def test_settings_carries_the_opt_out():
    """The switch is what makes showing real first names to other learners defensible. If
    it is not findable, the feature is not."""
    assert 'ratings_visible' in MAIN
    assert "setLeaderboardOptOut" in MAIN


def test_the_switch_reads_as_show_me_not_hide_me():
    """A switch that is ON when the thing is OFF is how people end up with their privacy
    setting backwards."""
    body = MAIN[MAIN.index("const lbCard"):]
    body = body[:body.index("wrap.append(lbCard);")]
    assert "const visible = !me.leaderboard_opt_out;" in body
    assert 'aria-checked", String(visible)' in body


def test_an_opted_out_learner_is_told_rather_than_shown_an_empty_board():
    body = MAIN[MAIN.index("function ratingsScreen"):]
    body = body[:body.index("\nasync function setLeaderboardOptOut")]
    assert "board.me.opted_out" in body
    assert "ratings_hidden" in body


# --- translated --------------------------------------------------------------

@pytest.mark.parametrize("lang", UI_LANGUAGES)
def test_every_string_exists_in_every_language(lang):
    strings = block(lang)
    for key in KEYS:
        assert strings.get(key, "").strip(), f"{lang} is missing {key}"


@pytest.mark.parametrize("key", KEYS)
def test_no_string_is_shared_between_languages(key):
    """The failure mode of every i18n edit here: one write landing in all four slots."""
    values = {lang: block(lang)[key] for lang in UI_LANGUAGES}
    assert len(set(values.values())) == len(values), f"{key} is shared: {values}"


def test_the_russian_strings_are_in_russian():
    strings = block("ru")
    for key in KEYS:
        if len(strings[key]) > 8:
            assert re.search(r"[А-Яа-яЁё]", strings[key]), \
                f"ru {key} has no Cyrillic: {strings[key]!r}"


def test_the_uzbek_strings_are_latin_script():
    """Modern Uzbek is Latin. A model or an author reaching for Cyrillic reads as wrong to
    the audience."""
    strings = block("uz")
    for key in KEYS:
        assert not re.search(r"[А-Яа-яЁё]", strings[key]), \
            f"uz {key} is in Cyrillic: {strings[key]!r}"
