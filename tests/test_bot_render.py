"""Message rendering for the companion bot.

Pure functions over an API response, so what text actually reaches the user is
testable without Telegram in the loop.

Question, verdict and explanation rendering left with the study loop when it moved
to the Mini App; what remains is progress, settings and subscription.
"""

import pytest

from bot import render
from bot.i18n import t

def test_stats_with_no_answers_points_at_the_app_not_a_command():
    """It used to say /quiz. That command no longer exists, so a new user following
    the instruction would have been met with silence."""
    data = {"questions_seen": 0, "questions_total": 7106, "answers_given": 0,
            "wrong": 0, "error_rate": 0.0, "boxes": {}, "by_topic": []}
    body = render.stats(data, "en")
    assert "/quiz" not in body
    assert "app" in body.lower()


def test_stats_shortens_the_ministerial_topic_names():
    long_name = ("Norme sulla circol. dei veicoli; pos. dei veicoli sulla carreggiata; "
                 "cambio di direz. di corsia (svolta)")
    data = {
        "questions_seen": 4, "questions_total": 7106, "answers_given": 10, "wrong": 3,
        "error_rate": 0.3, "boxes": {"1": 2},
        "by_topic": [{"topic_id": 1, "topic": long_name, "questions_seen": 4,
                      "answers_given": 10, "wrong": 3, "error_rate": 0.3}],
    }
    text = render.stats(data, "en")
    assert "Norme sulla circol. dei veicoli" in text
    assert "cambio di direz" not in text     # trailing clauses dropped
    assert "30%" in text


def test_stats_omits_topics_with_no_mistakes():
    data = {
        "questions_seen": 2, "questions_total": 7106, "answers_given": 2, "wrong": 0,
        "error_rate": 0.0, "boxes": {"2": 2},
        "by_topic": [{"topic_id": 1, "topic": "Distanza di sicurezza", "questions_seen": 2,
                      "answers_given": 2, "wrong": 0, "error_rate": 0.0}],
    }
    text = render.stats(data, "en")
    assert "Distanza" not in text


@pytest.mark.parametrize("lang", ["ru", "en", "it"])
def test_settings_renders_in_every_language(lang):
    user = {"lang": lang, "translations_on": True}
    text = render.settings(user, lang)
    assert len(text) > 20


# --- the on-demand explanation ---------------------------------------------


# --- plan ------------------------------------------------------------------

# `purchased` matters: a pass with no purchase behind it is the free trial, and /plan
# says something different for each. These fixtures predate the trial and were silently
# exercising the trial branch once it landed.
ACTIVE = {
    "chat_id": 1, "lang": "it", "translations_on": True,
    "pass_expires_at": "2026-08-29T10:30:00Z", "has_pass": True,
    "purchased": True, "free_explanations_left": 0,
}
LAPSED = {**ACTIVE, "pass_expires_at": None, "has_pass": False,
          "purchased": False, "free_explanations_left": 2}


def test_plan_shows_the_expiry_date_without_the_time():
    """A timestamp invites a timezone argument; the date is what a reader wants."""
    body = render.plan(ACTIVE, "it", can_subscribe=True)
    assert "2026-08-29" in body
    assert "10:30" not in body


@pytest.mark.parametrize("lang", ["it", "ru", "en"])
def test_plan_renders_in_every_language(lang):
    assert render.plan(ACTIVE, lang, can_subscribe=False)
    assert render.plan(LAPSED, lang, can_subscribe=True)



# --- /plan ------------------------------------------------------------------

TRIAL = {"has_pass": True, "purchased": False,
         "pass_expires_at": "2099-01-01T00:00:00+00:00"}
PAID = {"has_pass": True, "purchased": True,
        "pass_expires_at": "2026-08-29T10:30:00+00:00"}
FREE = {"has_pass": False, "purchased": False, "pass_expires_at": None}


def _prices() -> list[str]:
    """The published prices, from the constant rather than from memory.

    These used to be literals here, and a price rise meant editing four test files —
    each of which then asserted the new number against itself and proved nothing. Derived,
    they assert that what is RENDERED matches what is CONFIGURED, which is the only thing
    worth checking on this path.
    """
    from shared.constants import TIER_PRICE_CENTS

    return [f"{cents // 100}.{cents % 100:02d}" for cents in TIER_PRICE_CENTS.values()]


def test_plan_does_not_sell_to_someone_on_the_trial():
    """They already have everything. Pitching costs goodwill and buys nothing."""
    body = render.plan(TRIAL, "en", can_subscribe=True)
    assert not any(p in body for p in _prices())


def test_plan_does_not_sell_to_a_paying_subscriber():
    body = render.plan(PAID, "en", can_subscribe=True)
    assert not any(p in body for p in _prices())
    assert "2026-08-29" in body


def test_plan_shows_all_three_prices_to_a_free_user():
    body = render.plan(FREE, "en", can_subscribe=True)
    for price in _prices():
        assert f"€{price}" in body, f"missing {price}"


def test_plan_marks_the_featured_tier_and_orders_by_length():
    from shared.constants import TIER_DAYS, TIER_FEATURED

    body = render.plan(FREE, "en", can_subscribe=True)
    lines = [ln for ln in body.splitlines() if "€" in ln and "/mo" in ln]
    assert len(lines) == len(TIER_DAYS)
    assert "⭐" in lines[-1], "the longest plan should be last and featured"
    assert TIER_FEATURED == "pass_6m"


def test_plan_per_month_figures_are_right():
    """Computed from the price and the length, not typed. A stale per-month figure beside a
    fresh price is the most expensive kind of wrong number: it is the one people compare
    plans on, and it survives a price change silently."""
    from shared.constants import TIER_DAYS, TIER_PRICE_CENTS

    body = render.plan(FREE, "en", can_subscribe=True)
    for tier, cents in TIER_PRICE_CENTS.items():
        months = round(TIER_DAYS[tier] / 30)
        per = round(cents / months)
        assert f"€{per // 100}.{per % 100:02d}" in body, \
            f"{tier}: {cents} over {months} months is not shown per month"


def test_plan_says_payments_are_not_live_when_tribute_is_unconfigured():
    body = render.plan(FREE, "en", can_subscribe=False)
    assert t("en", "payments_not_live") in body


def test_a_trial_with_hours_left_still_reads_as_a_day():
    """Rounding down would say '0 days left' to someone who still has access, which
    reads as already over."""
    from datetime import datetime, timedelta, timezone

    soon = (datetime.now(timezone.utc) + timedelta(hours=11)).isoformat()
    body = render.plan({**TRIAL, "pass_expires_at": soon}, "en", can_subscribe=True)
    assert "1 days left" in body or "1 day" in body


@pytest.mark.parametrize("lang", ["it", "ru", "en", "uz"])
def test_plan_renders_in_every_language(lang):
    for state in (FREE, TRIAL, PAID):
        assert render.plan(state, lang, can_subscribe=True)
