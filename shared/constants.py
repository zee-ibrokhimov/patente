"""Constants shared by the API, the bot and the content pipeline."""

from __future__ import annotations

# --- Languages --------------------------------------------------------------
# Italian is the exam language and is never a "translation" — it is the source.
# Russian and English ship at launch; the schema takes more without a migration.
LANG_IT = "it"
LANG_RU = "ru"
LANG_EN = "en"
LANG_UZ = "uz"

# Three different lists, because they answer three different questions. They were two,
# and translations filtering on one while explanations filtered on the other is exactly
# how a language ends up half-enabled.
#
#   UI_LANGUAGES          — what the interface is available in.
#   TRANSLATION_LANGUAGES — what a QUESTION is translated into. Italian is absent because
#                           the question is already Italian; asking for an "it" translation
#                           is a paid call that can never produce a cacheable row.
#   EXPLANATION_LANGUAGES — what an EXPLANATION is written in. Uzbek is absent ON PURPOSE:
#                           a bad translation sits under the Italian where a user can see
#                           it is off, but a bad explanation is the only text on screen and
#                           is the thing being sold. Uzbek ships as translations first.
UI_LANGUAGES = (LANG_RU, LANG_EN, LANG_IT, LANG_UZ)
TRANSLATION_LANGUAGES = (LANG_RU, LANG_EN, LANG_UZ)
EXPLANATION_LANGUAGES = (LANG_IT, LANG_RU, LANG_EN)

# A UI language with no explanations of its own reads one in another language rather than
# being told nothing exists. Russian for Uzbek: it is the language this market already
# shares, and "unavailable" would be a worse answer than "available, in Russian".
EXPLANATION_FALLBACK = {LANG_UZ: LANG_RU}
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
# Three lengths. Plan §4.2's advice still holds — the middle option should carry the
# per-month framing and be the default, because the spread is visible but not obvious:
#   1 month   EUR 2.99   = 2.99/mo
#   3 months  EUR 7.99   = 2.66/mo
#   6 months  EUR 10.99  = 1.83/mo   <- best value, and the one to highlight
#
# ⚠️  ONE-TIME, NOT AUTO-RENEWING — for now. Plan §4.2 is explicit: "Both tiers must be
# one-time purchases, not auto-renewing. Keeping it one-time preserves the main advantage
# of the pass model — no subscription lifecycle, no cancellation flow, no failed-renewal
# handling, and no EU auto-renewal disclosure obligations."
#
# The product spec now asks for auto-renewal after a trial, which reverses that. It is
# NOT implemented here, because it is not a code change: it needs Tribute to support
# recurring billing, card capture before the trial, a cancellation path, dunning for
# failed renewals, and the EU pre-contractual disclosure the plan deliberately avoided —
# on top of the merchant-of-record and VAT questions §4.1 still lists as open.
TIER_1M = "pass_1m"
TIER_3M = "pass_3m"
TIER_6M = "pass_6m"
TIER_DAYS = {TIER_1M: 30, TIER_3M: 90, TIER_6M: 180}
TIER_PRICE_CENTS = {TIER_1M: 299, TIER_3M: 799, TIER_6M: 1099}
TIERS = tuple(TIER_DAYS)

# Tribute's `period` enum -> our tier. A subscription payload has no product id: the plan
# is identified by its billing cycle. Without this mapping every subscription falls
# through to the shortest tier, so a quarterly subscriber pays EUR 7.99 and is granted
# thirty days — the same defect the six-month product had before tier_for became
# data-driven, arriving by a different route.
TRIBUTE_PERIOD_TIERS = {
    "monthly": TIER_1M,
    "quarterly": TIER_3M,
    "halfyearly": TIER_6M,
}

# Periods Tribute can legitimately send that do not correspond to anything we sell.
# `trial` arrives on EVERY trial start, so without this the "unrecognised product"
# warning fires on a completely normal event — and a warning that cries wolf on the happy
# path is how the real one gets scrolled past later.
#
# Harmless to map them all to the shortest tier: for a subscription the expiry comes from
# Tribute's own `expires_at`, so the tier is only a label on the purchase row. If a
# yearly plan is ever sold, give it its own tier rather than leaving it here.
TRIBUTE_PERIODS_WITHOUT_TIER = ("trial", "onetime", "weekly", "yearly")
# The one to present as the default. Best per-month value, longest commitment.
TIER_FEATURED = TIER_6M


# --- Free trial --------------------------------------------------------------
# Every new user gets full Premium for a week, granted at first contact.
#
# This REPLACES the three-explanation taster. Three explanations was a sample of one
# feature; a week is a sample of the product — the user sees translations on every
# question, explanations whenever they want one, and their readiness score move. It is
# also far easier to explain, and it needs no payment details, so it works today rather
# than waiting on Tribute.
#
# NOW ZERO. Tribute owns the trial.
#
# This existed because it needed no payment details and so worked before Tribute did.
# Tribute's own 7-day trial went live on 2026-07-31 and grants access through the
# purchase webhook — which means a new user was getting BOTH: seven days here at first
# contact, then seven more from Tribute. Fourteen days free, and the internal one
# undercut the funnel by handing out the product before anyone was ever asked to pay.
#
# Observed on the owner's own test account: two `trial_started` events an hour apart.
#
# Left as a constant rather than deleted so it can be switched back on if Tribute is ever
# dropped. Changing it affects only NEW users; anyone mid-trial keeps the pass they have.
TRIAL_DAYS = 0


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


# --- Quiz sessions ----------------------------------------------------------
# Two modes, and the difference is what the user is being asked to do.
#
# EXAM is a measurement: a fixed paper, a clock, and nothing revealed until it is over.
# PRACTICE is study: no clock, ends when the user says so, explanation after each answer.
#
# The parameters are constants rather than literals because plan §11 explicitly leaves
# the format open — "settle the exam format question (30 vs 40 questions) while you have
# easy access to a real autoscuola — sources disagree and your simulator depends on it".
# Changing the exam is meant to be an edit here, not a refactor.
#
# EXAM_MAX_ERRORS is the number TOLERATED. Plan §2 says "fail at 4 errors", so three
# mistakes still passes and the fourth does not.
MODE_EXAM = "exam"
MODE_PRACTICE = "practice"
QUIZ_MODES = (MODE_EXAM, MODE_PRACTICE)

EXAM_QUESTIONS = 30

# Practice has no fixed length: it runs until the learner ends it. Questions are drawn in
# batches rather than all 7106 at once — the paper travels to the client whole, and a
# megabyte of JSON to start a session nobody may finish is the wrong trade. When the
# learner reaches the end of a batch the sitting extends itself.
PRACTICE_BATCH = 30
EXAM_MINUTES = 20
EXAM_MAX_ERRORS = 3

SESSION_OPEN = "open"
SESSION_SUBMITTED = "submitted"
SESSION_EXPIRED = "expired"
SESSION_ABANDONED = "abandoned"
SESSION_STATES = (SESSION_OPEN, SESSION_SUBMITTED, SESSION_EXPIRED, SESSION_ABANDONED)

# An exam measures; it does not teach. Feeding its answers to the Leitner scheduler
# would let a pressured guess promote a question into the 7- or 30-day box, and would
# re-stamp thirty due_at values to the exam's clock — which then dominates the practice
# queue, because selection orders strictly by due_at. The corruption is silent and only
# becomes visible weeks later, so this is a decision made once, here.
MODE_UPDATES_SCHEDULE = {MODE_EXAM: False, MODE_PRACTICE: True}
# Exam mode reveals nothing until the end, so it must not touch the explanation path at
# all: `explanations.deliver` is the one place a free taster is spent and the one place
# EV_PAYWALL_HIT is written, and an exam would fire thirty of each for text never shown.
MODE_OFFERS_EXPLANATION = {MODE_EXAM: False, MODE_PRACTICE: True}


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
# The free trial, kept distinct from both a purchase and an admin grant. "Converted after
# a trial" is the number that decides whether the trial works, and it is only separable if
# these are different events from the very first user.
EV_TRIAL_STARTED = "trial_started"
# A subscriber turned off auto-renewal. NOT a revocation: they keep what they paid for
# until it lapses. Recorded because otherwise churn leaves no trace at all — the only
# symptom would be a renewal that never arrived.
EV_SUBSCRIPTION_CANCELLED = "subscription_cancelled"
# A learner wiped their own progress. Recorded because "my stats vanished" is a support
# message someone will send after forgetting they did this.
EV_PROGRESS_RESET = "progress_reset"
# Quiz sessions. EV_SESSION_START/END already existed for the study session; these name
# the bounded, gradeable kind so an exam is separable in the funnel.
EV_EXAM_STARTED = "exam_started"
EV_EXAM_FINISHED = "exam_finished"
EVENT_TYPES = (
    EV_QUESTION_SERVED, EV_ANSWER_GIVEN, EV_TRANSLATION_TOGGLED, EV_EXPLANATION_VIEWED,
    EV_PAYWALL_HIT, EV_PAYWALL_DISMISSED, EV_PURCHASE_STARTED, EV_PURCHASE_COMPLETED,
    EV_PURCHASE_REFUNDED, EV_SESSION_START, EV_SESSION_END, EV_REPORT_SUBMITTED,
    EV_USER_DELETED,
    EV_PASS_GRANTED,
    EV_EXAM_STARTED, EV_EXAM_FINISHED,
    EV_TRIAL_STARTED,
    EV_SUBSCRIPTION_CANCELLED,
    EV_PROGRESS_RESET,
)

# --- vocabulary trainer -----------------------------------------------------

# Terms per round. Twenty typed answers is roughly three minutes, which fits the gap
# the app is used in — a bus ride — and ends while the learner still wants another.
VOCAB_ROUND_SIZE = 20

# Which way round a term is asked. The round mixes both, because recognising `sosta`
# and producing it from `parking` are different skills and only one of them is what the
# written exam demands.
VOCAB_IT_TO_LANG = "it_to_lang"
VOCAB_LANG_TO_IT = "lang_to_it"
VOCAB_DIRECTIONS = (VOCAB_IT_TO_LANG, VOCAB_LANG_TO_IT)

# The language a learner is tested against. Italian is excluded for the same reason it is
# excluded from TRANSLATION_LANGUAGES: an it/it pair is not a question. An Italian-UI
# user is tested against English.
VOCAB_PAIR_FALLBACK = LANG_EN
