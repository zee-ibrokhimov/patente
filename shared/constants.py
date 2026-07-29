"""Constants shared by the API, the bot and the content pipeline."""

from __future__ import annotations

# --- Languages --------------------------------------------------------------
# Italian is the exam language and is never a "translation" — it is the source.
# Russian and English ship at launch; the schema takes more without a migration.
LANG_IT = "it"
LANG_RU = "ru"
LANG_EN = "en"
UI_LANGUAGES = (LANG_RU, LANG_EN, LANG_IT)
TRANSLATION_LANGUAGES = (LANG_RU, LANG_EN)
DEFAULT_LANG = LANG_RU


# --- Explanation review status ----------------------------------------------
# A topic goes live only when every explanation in it is APPROVED. An absent
# explanation is acceptable; a confidently wrong one is not.
STATUS_DRAFT = "draft"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_FLAGGED = "flagged"  # auto-flagged for review: contains a number, or
                            # argues against the stored answer
EXPLANATION_STATUSES = (STATUS_DRAFT, STATUS_APPROVED, STATUS_REJECTED, STATUS_FLAGGED)


# --- Purchase tiers ---------------------------------------------------------
# Both one-time, never auto-renewing: no subscription lifecycle, no cancellation
# flow, no EU auto-renewal disclosure obligations.
TIER_1M = "pass_1m"
TIER_3M = "pass_3m"
TIER_DAYS = {TIER_1M: 30, TIER_3M: 90}
TIER_PRICE_CENTS = {TIER_1M: 299, TIER_3M: 699}
TIERS = tuple(TIER_DAYS)


# --- Leitner boxes ----------------------------------------------------------
# Box 1 is "just got it wrong", box 5 is "solid". A wrong answer always drops to
# box 1 — the whole point is that wrong answers come back soon.
LEITNER_BOXES = 5
LEITNER_INTERVALS_HOURS = {1: 0, 2: 12, 3: 48, 4: 168, 5: 720}
FIRST_BOX = 1


# --- Event log (plan §9) ----------------------------------------------------
# Instrumented from the first commit because none of it can be backfilled.
# Everything reported later is derived from these.
EV_QUESTION_SERVED = "question_served"
EV_ANSWER_GIVEN = "answer_given"
EV_TRANSLATION_TOGGLED = "translation_toggled"
EV_EXPLANATION_VIEWED = "explanation_viewed"
EV_PAYWALL_HIT = "paywall_hit"
EV_PAYWALL_DISMISSED = "paywall_dismissed"
EV_PURCHASE_STARTED = "purchase_started"
EV_PURCHASE_COMPLETED = "purchase_completed"
EV_PURCHASE_REFUNDED = "purchase_refunded"
EV_SESSION_START = "session_start"
EV_SESSION_END = "session_end"
EV_REPORT_SUBMITTED = "report_submitted"
EV_USER_DELETED = "user_deleted"
EVENT_TYPES = (
    EV_QUESTION_SERVED, EV_ANSWER_GIVEN, EV_TRANSLATION_TOGGLED, EV_EXPLANATION_VIEWED,
    EV_PAYWALL_HIT, EV_PAYWALL_DISMISSED, EV_PURCHASE_STARTED, EV_PURCHASE_COMPLETED,
    EV_PURCHASE_REFUNDED, EV_SESSION_START, EV_SESSION_END, EV_REPORT_SUBMITTED,
    EV_USER_DELETED,
)
