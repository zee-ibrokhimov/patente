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
    # The rules screen. Added to KEYS or the four-language checks below are no-ops for them —
    # a new string that only exists in English fails nothing until an Uzbek learner opens it.
    "league_rules_title", "league_medal_hint",
    "league_rule_season", "league_rule_season_body",
    "league_rule_points", "league_rule_points_body",
    "league_rule_prizes", "league_rule_prizes_body",
    "league_rule_seen", "league_rule_seen_body",
    "league_rule_why", "league_rule_why_body",
    "league_point_earned",
)


def block_of(name: str) -> str:
    """One function's body, stopping at the next top-level function.

    Stops at `async function` too: a version that did not would run past the end of an async
    function and let an assertion pass on a completely different one further down the file.
    """
    start = MAIN.index(f"function {name}(")
    end = len(MAIN)
    for marker in ("\nfunction ", "\nasync function "):
        at = MAIN.find(marker, start + 10)
        if at != -1:
            end = min(end, at)
    return MAIN[start:end]


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
    setting backwards.

    Re-anchored: this toggle used to be its own card, and the three one-toggle cards in
    settings are now one grouped list — three cards, three paddings and two 12px gaps came
    to 427px of a 730px phone for three switches. The property is unchanged and so is the
    slice; only the markers moved."""
    body = MAIN[MAIN.index("const lbRow = el(\"div\", \"row\");"):]
    body = body[:body.index("prefs.append(lbRow);")]
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


# --- medals ------------------------------------------------------------------

def test_a_medal_is_its_own_element_and_not_part_of_the_name():
    """THE spoofing guard.

    Telegram first names are whatever the person typed, and the sanitiser deliberately keeps
    emoji — stripping them mangles ordinary names. So someone renaming themselves
    "\U0001f947 Aziz" costs one tap and no studying. The defence is structural: the medal is
    its own field on the row and its own element in the DOM, so a fake one sits visibly in
    the wrong place instead of where a real one would be.
    """
    body = block_of("ratingsScreen")
    assert "medalMark(entry.medal)" in body, "the medal is not rendered as its own element"
    # The name is rendered on its own, never concatenated with a medal.
    assert 'entry.name || t("ratings_anon")' in body
    for bad in ('entry.medal + entry.name', 'entry.name + entry.medal',
                '${entry.medal}${entry.name}', '${entry.name}${entry.medal}'):
        assert bad not in MAIN, f"a medal was concatenated into a name: {bad}"


def test_the_medal_is_not_drawn_in_gold():
    """tokens.css reserves gold exclusively for Premium — "the moment gold means two things
    it stops working" — and a league placing is not a purchase."""
    rules = re.search(r"^\.medal \{(.*?)\}", CSS, re.M | re.S)
    assert rules, "no .medal rule"
    assert "--gold" not in rules.group(1)


def test_the_rules_are_reachable_from_the_board():
    """Not buried in Settings. The people who need them are looking at the board right now,
    asking why their points did not move."""
    assert "openLeagueRules()" in block_of("ratingsScreen")


def test_the_rules_explain_why_points_do_not_move():
    """The card that decides the support load. Three separate rules can silently make a
    correct answer worth nothing, and a learner who is not told cannot tell any of them
    apart from a bug."""
    body = block_of("openLeagueRules")
    assert "league_rule_why" in body and "league_rule_why_body" in body


def test_every_rules_string_says_the_same_numbers_the_server_uses():
    """A rules page quoting a cap the server does not enforce is worse than no rules page."""
    from shared.constants import LEAGUE_DAILY_ANSWER_CAP, LEAGUE_EXAM_BONUS

    for lang in ("it", "ru", "en", "uz"):
        text = block(lang)["league_rule_points_body"]
        assert str(LEAGUE_DAILY_ANSWER_CAP) in text, f"{lang} does not state the daily cap"
        assert str(LEAGUE_EXAM_BONUS) in text, f"{lang} does not state the exam bonus"


def test_the_learner_is_told_when_an_answer_scores():
    """The line that stops the support messages.

    Without it, a correct answer that scored nothing — already counted this week, day capped,
    pace not credited — is indistinguishable from a broken board. Rendered only when it
    actually scored, so its absence is itself the signal to go and read the rules.
    """
    body = block_of("verdictBox")
    assert "a.league_point" in body
    assert "league_point_earned" in body
