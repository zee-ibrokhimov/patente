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

# What a user may be shown. Explanations are generated on request now, so the first
# reader of a draft is a paying user rather than a reviewer (STATUS.md §13) — which
# makes the automatic gates the only quality bar standing between the model and them.
# A draft passed every gate; a flagged one did not, and reads as "not written yet"
# rather than as an answer. Removing STATUS_DRAFT here restores plan §3.3's original
# rule that nothing unread by a human is ever served.
SERVABLE_STATUSES = (STATUS_APPROVED, STATUS_DRAFT)


# --- Purchase tiers ---------------------------------------------------------
# Both one-time, never auto-renewing: no subscription lifecycle, no cancellation
# flow, no EU auto-renewal disclosure obligations.
TIER_1M = "pass_1m"
TIER_3M = "pass_3m"
TIER_DAYS = {TIER_1M: 30, TIER_3M: 90}
TIER_PRICE_CENTS = {TIER_1M: 299, TIER_3M: 699}
TIERS = tuple(TIER_DAYS)


# --- Leitner boxes ----------------------------------------------------------
# Box 1 is "just got it wrong", box 5 is "solid". A wrong answer always drops
# straight back to box 1 — the whole point is that wrong answers come back soon.
#
# Box 1 is 10 minutes rather than 0 so a missed question returns within the same
# study session but with roughly ten others in between. At 0 it would be the very
# next question served, which teaches recall of the last screen, not the rule.
LEITNER_BOXES = 5
FIRST_BOX = 1
LEITNER_INTERVALS_MINUTES = {
    1: 10,        # same session
    2: 12 * 60,
    3: 2 * 24 * 60,
    4: 7 * 24 * 60,
    5: 30 * 24 * 60,
}


# --- Free vs paid -----------------------------------------------------------
# Settled in plan §4.3: free is the plain Italian question, Vero/Falso and the
# correct answer. Translations and explanations are paid, full stop.
#
# Spaced repetition and stats are listed there as *recommended* paid, not
# decided. They are free for now: both cost nothing to serve, and a free user who
# keeps coming back hits the explanation paywall far more often than one who
# drills unsorted questions once and leaves. Flip these to gate them.
REQUIRE_PASS_FOR_SPACED_REPETITION = False
REQUIRE_PASS_FOR_STATS = False


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
# A pass given by hand, never by payment. Kept distinct from EV_PURCHASE_COMPLETED so a
# comped tester or a repaired webhook cannot be mistaken for someone deciding to pay.
EV_PASS_GRANTED = "pass_granted"
EVENT_TYPES = (
    EV_QUESTION_SERVED, EV_ANSWER_GIVEN, EV_TRANSLATION_TOGGLED, EV_EXPLANATION_VIEWED,
    EV_PAYWALL_HIT, EV_PAYWALL_DISMISSED, EV_PURCHASE_STARTED, EV_PURCHASE_COMPLETED,
    EV_PURCHASE_REFUNDED, EV_SESSION_START, EV_SESSION_END, EV_REPORT_SUBMITTED,
    EV_USER_DELETED,
    EV_PASS_GRANTED,
)
