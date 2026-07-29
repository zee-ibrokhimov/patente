"""Message rendering.

Pure functions over an API response, so the bot's most important behaviour — what
text actually reaches the user — is testable without Telegram in the loop.
"""

import pytest

from bot import render

QUESTION = {
    "id": 1,
    "statement_it": "Il segnale raffigurato vieta il transito",
    "stem_it": None,
    "image": "images/a.jpeg",
    "translation_state": "locked",
    "translation": None,
}
TRANSLATED = {
    **QUESTION,
    "translation_state": "shown",
    "translation": {"lang": "ru", "statement": "Знак запрещает движение"},
}


def outcome(**kw):
    base = {
        "question_id": 1, "given": False, "correct": False, "correct_answer": True,
        "box": 1, "explanation_state": "unavailable", "explanation": None,
    }
    return {**base, **kw}


# --- questions -------------------------------------------------------------

def test_italian_is_always_shown():
    assert "Il segnale raffigurato vieta il transito" in render.question(QUESTION, "ru")


def test_locked_translation_is_not_rendered():
    assert "Знак" not in render.question(QUESTION, "ru")


def test_shown_translation_sits_under_the_italian_in_italics():
    text = render.question(TRANSLATED, "ru")
    italian = text.index("Il segnale")
    russian = text.index("Знак")
    assert italian < russian, "the exam wording must come first"
    assert "<i>Знак запрещает движение</i>" in text


def test_html_in_the_statement_is_escaped():
    """Ministerial text contains '<' and '&'; unescaped it breaks the message."""
    q = {**QUESTION, "statement_it": "Massa < 3,5 t & rimorchio"}
    text = render.question(q, "ru")
    assert "&lt; 3,5 t &amp; rimorchio" in text


def test_stem_is_rendered_above_the_statement_when_present():
    q = {**QUESTION, "stem_it": "Il conducente deve"}
    text = render.question(q, "ru")
    assert text.index("Il conducente deve") < text.index("Il segnale")


def test_long_text_is_clipped_to_the_caption_limit():
    q = {**QUESTION, "statement_it": "x" * 2000}
    text = render.question(q, "ru")
    assert len(text) <= render.CAPTION_LIMIT
    assert text.endswith("…")


# --- results ---------------------------------------------------------------

def test_wrong_answer_reveals_the_correct_one():
    """The free tier still gets this — it is the whole free product (§4.3)."""
    text = render.result(QUESTION, outcome(correct=False, correct_answer=False), "en")
    assert "Wrong" in text and "FALSO" in text


def test_correct_answer_does_not_repeat_the_answer():
    text = render.result(QUESTION, outcome(correct=True), "en")
    assert "Correct" in text


def test_statement_stays_visible_after_answering():
    text = render.result(QUESTION, outcome(), "en")
    assert "Il segnale raffigurato vieta il transito" in text


def test_shown_explanation_is_rendered():
    text = render.result(
        QUESTION, outcome(explanation_state="shown", explanation="Perché lo dice l'art. 116."),
        "it",
    )
    assert "art. 116" in text


def test_locked_explanation_shows_the_paywall():
    text = render.result(QUESTION, outcome(explanation_state="locked"), "en")
    assert "🔒" in text


def test_unavailable_explanation_shows_nothing_at_all():
    """Never offer to sell an explanation nobody has written (§3.3)."""
    text = render.result(QUESTION, outcome(explanation_state="unavailable"), "en")
    assert "🔒" not in text
    assert "pass" not in text.lower()


# --- stats -----------------------------------------------------------------

def test_stats_with_no_answers_prompts_to_start():
    data = {"questions_seen": 0, "questions_total": 7106, "answers_given": 0,
            "wrong": 0, "error_rate": 0.0, "boxes": {}, "by_topic": []}
    assert "/quiz" in render.stats(data, "en")


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
