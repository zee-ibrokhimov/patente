"""Message rendering for the companion bot.

Pure functions over an API response, so what text actually reaches the user is
testable without Telegram in the loop.

Question, verdict and explanation rendering left with the study loop when it moved
to the Mini App; what remains is progress, settings and subscription.
"""

import pytest

from bot import render

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

ACTIVE = {
    "chat_id": 1, "lang": "it", "translations_on": True,
    "pass_expires_at": "2026-08-29T10:30:00Z", "has_pass": True,
    "free_explanations_left": 0,
}
LAPSED = {**ACTIVE, "pass_expires_at": None, "has_pass": False, "free_explanations_left": 2}


def test_plan_shows_the_expiry_date_without_the_time():
    """A timestamp invites a timezone argument; the date is what a reader wants."""
    body = render.plan(ACTIVE, "it", can_subscribe=True)
    assert "2026-08-29" in body
    assert "10:30" not in body


def test_plan_without_a_pass_shows_the_free_allowance():
    body = render.plan(LAPSED, "it", can_subscribe=True)
    assert "2" in body
    assert "2026-08-29" not in body


def test_plan_says_payments_are_not_live_when_tribute_is_unconfigured():
    """Better than a Buy button that opens nothing — unfinished, not broken."""
    from bot.i18n import t

    body = render.plan(LAPSED, "it", can_subscribe=False)
    assert t("it", "payments_not_live") in body


def test_plan_omits_that_notice_once_payments_are_live():
    from bot.i18n import t

    body = render.plan(LAPSED, "it", can_subscribe=True)
    assert t("it", "payments_not_live") not in body


@pytest.mark.parametrize("lang", ["it", "ru", "en"])
def test_plan_renders_in_every_language(lang):
    assert render.plan(ACTIVE, lang, can_subscribe=False)
    assert render.plan(LAPSED, lang, can_subscribe=True)
