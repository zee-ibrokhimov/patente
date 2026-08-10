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
#   EXPLANATION_LANGUAGES — what an EXPLANATION is written in.
#
# Uzbek used to be absent from the last one on purpose, and the reasoning was: a bad
# translation sits under the Italian where a reader can see it is off, but a bad
# explanation is the only text on screen and is the thing being sold. So Uzbek shipped as
# translations first and read its explanations in Russian.
#
# Reversed on the owner's instruction (2026-07-31), and they are right. Someone who set the
# app to Uzbek gets Uzbek questions, Uzbek vocabulary and an Uzbek interface, and then the
# one screen they actually have to READ arrives in another language. That does not read as
# "a language we are careful about"; it reads as broken. It also asks a group who chose
# Uzbek over Russian to use Russian for the paid feature.
UI_LANGUAGES = (LANG_RU, LANG_EN, LANG_IT, LANG_UZ)
TRANSLATION_LANGUAGES = (LANG_RU, LANG_EN, LANG_UZ)
EXPLANATION_LANGUAGES = (LANG_IT, LANG_RU, LANG_EN, LANG_UZ)

# The safety net, not the plan. Uzbek explanations are written now, so this fires only when
# a specific cluster has no servable Uzbek row — the model declined that language, or a
# gate withheld it. Showing a Russian explanation beats showing nothing, and the response
# carries `explanation_lang` so the reader is told which language they got.
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


# --- Topic families ---------------------------------------------------------
# The ministry stamps one of 25 topics on every question, and that is the vocabulary every
# Italian study book uses — so the topics are kept, and grouped for a screen that has to fit
# on a phone and be read in ten seconds.
#
# SEVEN, not the three the owner remembered. His memory is real: the highway code does split
# VERTICAL signs into danger, prescription and information, and that is family 1 below. But
# another 950 sign questions — road markings, traffic lights, police signals, supplementary
# panels, roadworks — are not vertical signs and would have nowhere to go in a three-way
# split, so they are their own family.
#
# The counts are exact against the seeded bank (7,106 questions) and were verified before
# these groups were written down. Because an exam draws uniformly from the whole bank, a
# family's share of the bank IS its expected share of a 30-question paper — which is what
# makes "you would average N mistakes" computable rather than a guess.
#
#   1 signs_vertical   2,436   34.3%   10.3 questions per exam
#   2 rules            1,724   24.3%    7.3
#   3 signs_other        950   13.4%    4.0
#   4 vehicle            696    9.8%    2.9
#   5 definitions        531    7.5%    2.2
#   6 safety             401    5.6%    1.7
#   7 documents          368    5.2%    1.6
#
# Every topic id appears exactly once. `test_every_topic_has_a_family` enforces both halves
# of that — nothing missing, nothing counted twice — because a topic silently absent from
# this map would vanish from the screen with no error anywhere.
TOPIC_FAMILIES: dict[str, tuple[int, ...]] = {
    "signs_vertical": (20, 22, 23, 24, 21),
    "rules": (6, 13, 9, 7, 11, 12),
    "signs_other": (14, 17, 18, 19),
    "vehicle": (5, 3, 25, 10),
    "definitions": (2,),
    "safety": (1, 4, 8),
    "documents": (15, 16),
}

# topic id -> family, the direction every query actually needs.
FAMILY_OF_TOPIC: dict[int, str] = {
    topic: family for family, topics in TOPIC_FAMILIES.items() for topic in topics
}

# The window the headline error rate is measured over.
#
# Not all-time: it barely moves once a learner has a few hundred answers behind them, so the
# number stops rewarding improvement exactly when improvement starts. Not one sitting: 30
# answers swing about fourteen points on luck alone, and the same learner would read 6% one
# evening and 34% the next.
#
# 100 is ~3.3 exams, and it is the window `profile._readiness` already uses. Two screens
# quoting accuracy over two different windows is two screens that contradict each other, and
# the learner cannot tell which to believe.
ERROR_WINDOW = 100

# Below this, no percentage is shown at all. A rate over twelve answers is a guess being
# told to somebody deciding whether to book a paid exam.
ERROR_MIN_SAMPLE = 100

# A topic needs this many distinct questions answered before it gets a percentage. Below
# ten, the margin of error is wider than the useful range of the metric.
TOPIC_MIN_SAMPLE = 10

# How far back a topic breakdown looks. The lifetime tally that ships today never recovers:
# a question missed four times last month and fixed today drags its topic down forever, with
# no action the learner can take to clear it.
TOPIC_WINDOW_DAYS = 90

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

# How long to grant a TRIAL whose `expires_at` we could not read.
#
# Tribute's date is authoritative and this should never be needed. But when it is
# unreadable the code fell through to TIER_DAYS for the tier — and a trial has no tier, so
# it took the shortest PAID one: thirty days free instead of seven, from a payload we
# could not understand. Guessing generously with someone else's product is the wrong
# instinct; this matches what the Tribute subscription is actually configured with.
TRIBUTE_TRIAL_FALLBACK_DAYS = 7
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


# --- the leaderboard --------------------------------------------------------
# A weekly league, Monday to Sunday in UTC.
#
# Weekly rather than all-time because an all-time board is won permanently by whoever
# arrived first, and nobody who joins later can ever place — which is the opposite of an
# engagement mechanic. A fixed week also creates the deadline that makes it work, and needs
# no reset job: "this week" is a WHERE clause on the answer log.
#
# Ranked on CORRECT answers, not answers given. Counting attempts rewards tapping quickly
# and being wrong, on a product whose entire pitch is understanding the question.
LEADERBOARD_SIZE = 15
# Below this many ranked learners the board is hiding more than it shows — with four users
# somebody is permanently last, which is demoralising rather than motivating. The API still
# returns the rows; the client is told it is too quiet to display as a competition.
LEADERBOARD_MIN_PLAYERS = 5


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

# --- repeat rounds ----------------------------------------------------------
# What a practice sitting DRAWS FROM. Only the draw changes: a repeat round records answers
# and updates the Leitner schedule exactly like any other practice, because the learner is
# genuinely studying.
#
# Asked for directly by the owner. `practice_paper` already resurfaces mistakes, but on the
# spaced-repetition schedule — when the algorithm decides, mixed with new material. That is
# the right default and the wrong tool for "my test is Friday, show me everything I have got
# wrong". Revision to a deadline wants a deliberate pass over a known set.
REPEAT_SMART = "smart"      # the default: due, then unseen, then not-yet-due
REPEAT_WRONG = "wrong"      # every question ever answered incorrectly
REPEAT_CORRECT = "correct"  # seen, and never once wrong
REPEAT_SOURCES = (REPEAT_SMART, REPEAT_WRONG, REPEAT_CORRECT)

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
# Premium is about to end / has ended. Recorded rather than flagged on the user, so a pass
# that lapses, renews and lapses again is announced each time without a flag anyone has to
# remember to clear.
EV_PASS_ENDING = "pass_ending"
EV_PASS_LAPSED = "pass_lapsed"
# A streak freeze was spent, and which DAY it covered. In the event log rather than a
# "frozen until" column for the same reason as the lapse notices: a column has to be cleared
# correctly every time, and getting that wrong means either an eternal streak or a freeze
# that silently does nothing. Keyed on the day, so computing the streak twice cannot spend
# two freezes.
EV_STREAK_FROZEN = "streak_frozen"
# A 14-day streak milestone was reached and paid for. Carries the milestone NUMBER, so the
# payout is idempotent: the second pass over the same milestone finds it already recorded and
# grants nothing. The grant itself also writes EV_PASS_GRANTED, which is what the revenue
# reports read; this event exists to make the payout repeatable-safe and to say which
# milestone bought it.
EV_STREAK_MILESTONE = "streak_milestone"
# A newsletter or a personal message went out from the admin panel. In the event log rather
# than a table of its own: it is append-only, already backed up, and "did I already send
# this" is exactly the question somebody needs answered before sending again. It is also the
# only record that a newsletter happened at all.
EV_BROADCAST_SENT = "broadcast_sent"
# A referral link was created or switched off. Who gave away access, and when.
EV_LINK_CHANGED = "link_changed"
# A learner's "this explanation is wrong" was dealt with, and how — read and dismissed, or
# the cluster rewritten. Recorded because the share of reports that turn out to be RIGHT is
# the only honest measure of explanation quality this product has: no human reviewed a draft
# before its first reader did, so the readers ARE the review.
EV_REPORT_RESOLVED = "report_resolved"
# Access was ended or shortened by hand. Carries the PREVIOUS expiry, which is what makes a
# mistaken revoke undoable — re-granting the difference restores it exactly.
EV_PASS_REVOKED = "pass_revoked"

# What a hand sale's `tribute_purchase_id` starts with.
#
# The column is UNIQUE and NOT NULL and there is no Tribute any more, so the grant route
# puts a synthetic id there. It is a shared constant rather than a literal in two files
# because lapse.py READS it to tell a manual sale from a real subscription — and when those
# two disagree, paying customers stop being warned that their access is ending.
MANUAL_PURCHASE_PREFIX = "manual:"

# One paid model call, with its token counts. Every call already COMPUTED these and every
# caller threw them away, so the running cost of the product was unknowable from inside it —
# the only figure available was whatever OpenAI's dashboard said a week later, with no way
# to attribute it. Recorded as an event rather than a table: it is append-only, it is
# already backed up, and spend is a question asked once a month, not a hot path.
EV_MODEL_CALL = "model_call"

# A come-back nudge was attempted. Recorded whether or not it was delivered: a blocked bot
# fails every time, and the limits in api/services/reminders.py count ATTEMPTS, so a record
# only of successes would keep a blocked user permanently at the front of the queue.
EV_REMINDER_SENT = "reminder_sent"

# A vocabulary card was self-graded — "I knew it" or "I didn't". Recorded with the mode on
# it because a box reached by tapping is weaker evidence than one reached by typing:
# recognition is easier than recall. Nothing depends on the distinction today; this is what
# makes it measurable if the vocabulary numbers ever start looking better than the learner.
EV_VOCAB_RECALL = "vocab_recall"
EV_VOCAB_ADDED = "vocab_added"

# Quiz sessions. EV_SESSION_START/END already existed for the study session; these name
# the bounded, gradeable kind so an exam is separable in the funnel.
EV_EXAM_STARTED = "exam_started"
EV_EXAM_FINISHED = "exam_finished"
EVENT_TYPES = (
    EV_QUESTION_SERVED, EV_ANSWER_GIVEN, EV_TRANSLATION_TOGGLED, EV_EXPLANATION_VIEWED,
    EV_PAYWALL_HIT, EV_PAYWALL_DISMISSED, EV_PURCHASE_STARTED, EV_PURCHASE_COMPLETED,
    EV_PURCHASE_REFUNDED, EV_SESSION_START, EV_SESSION_END, EV_REPORT_SUBMITTED,
    EV_REPORT_RESOLVED,
    EV_PASS_REVOKED,
    EV_USER_DELETED,
    EV_PASS_GRANTED,
    EV_EXAM_STARTED, EV_EXAM_FINISHED,
    EV_TRIAL_STARTED,
    EV_SUBSCRIPTION_CANCELLED,
    EV_PROGRESS_RESET,
    EV_PASS_ENDING,
    EV_PASS_LAPSED,
    EV_STREAK_FROZEN,
    EV_STREAK_MILESTONE,
    EV_BROADCAST_SENT,
    EV_LINK_CHANGED,
    EV_VOCAB_ADDED,
    EV_VOCAB_RECALL,
)
# What counts as the USER doing something, as opposed to something being done TO them.
#
# The event log holds both. `reminder_sent`, `pass_granted` and `broadcast_sent` are the
# system writing about a person, and counting those as activity is circular: a reminder
# makes its own recipient look active for the next ten days, so they are never eligible
# again — which is exactly how the reminder gap silently stopped mattering. The same defect
# already inflates `active_24h` on the admin overview, where the owner sending a newsletter
# registers as active users.
USER_ACTIVITY_EVENTS = (
    EV_QUESTION_SERVED,
    EV_ANSWER_GIVEN,
    EV_EXPLANATION_VIEWED,
    EV_SESSION_START,
    EV_EXAM_STARTED,
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
