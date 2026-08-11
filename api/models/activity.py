"""Per-user state: users, progress, purchases, events, reports.

This is the only irreplaceable data in the system — content can be regenerated
from the listato, entitlement and progress cannot. It is also the only place
personal data lives, and the answer to "what personal data?" is deliberately
"a Telegram chat id and an answer history". No names, usernames or message text
are stored anywhere, which is what makes /delete a single cascading delete.

Deviations from the plan's §14.3 sketch, and why:

  · `progress` gets an explicit composite primary key (chat_id, question_id). The
    sketch had no key at all, which would have allowed a user to accumulate
    duplicate rows for the same question and quietly corrupt their own Leitner
    scheduling.

  · `events.payload` is a JSON column rather than free text, so the §9 metrics
    stay queryable without a migration or a parsing pass.

  · `purchases.tribute_purchase_id` is UNIQUE and NOT NULL. That constraint is
    the webhook idempotency guarantee: a redelivered payment fails to insert
    instead of extending the pass a second time.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from api.models.base import Base, utcnow


class User(Base):
    __tablename__ = "users"

    # Telegram chat id. Exceeds 32 bits for newer accounts, hence BigInteger.
    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    lang: Mapped[str] = mapped_column(Text, default="ru")
    translations_on: Mapped[bool] = mapped_column(default=True)

    # The language QUESTIONS are translated into, when it differs from the interface.
    #
    # NULL means "follow `lang`", which is what it did when there was only one field. Kept
    # separate because the two are genuinely different choices: Uzbek shipped as beta and a
    # good number of its speakers read Russian more comfortably, so making them switch the
    # whole app to Russian to read Russian translations is the wrong trade.
    translation_lang: Mapped[str | None] = mapped_column(Text, default=None)

    # NULL means "never bought". Access is granted while this is in the future;
    # on expiry, progress is kept and only translations and explanations lock.
    pass_expires_at: Mapped[datetime | None] = mapped_column(default=None, index=True)

    # Lifetime taster (plan §4.3). Quality is the entire pitch, so a user has to
    # see a good explanation before being asked to pay for them.
    free_explanations_used: Mapped[int] = mapped_column(default=0)

    # Membership of the Premium channel, cached. Telegram is the authority and asking it
    # costs an API call, so the answer is stored and refreshed on a TTL rather than
    # checked on every request.
    #
    # It is a SECOND source of entitlement beside the Tribute pass, not a requirement
    # alongside it. Tribute adds buyers to the channel itself, so membership survives a
    # webhook we never received — which is not hypothetical: a three-hour outage on
    # 2026-07-31 meant deliveries were refused with 530 while people were in the channel
    # and paid up. Requiring BOTH would have cut those people off.
    #
    # One of: creator | administrator | member | restricted | left | kicked | unknown.
    # Where this person came from: the payload on their very first /start deep link,
    # e.g. t.me/quizpatente_bot?start=tg_uzbeks_italy. Written ONCE, at first contact,
    # and never overwritten — a user who later arrives through a different link has still
    # been acquired by the first one, and letting the newest link claim them would credit
    # whichever channel they happened to revisit.
    #
    # Impossible to reconstruct later, which is the argument for adding it before there
    # is any traffic rather than after.
    source: Mapped[str | None] = mapped_column(Text, default=None)

    channel_status: Mapped[str | None] = mapped_column(Text, default=None)
    channel_checked_at: Mapped[datetime | None] = mapped_column(default=None)

    # --- the leaderboard ----------------------------------------------------
    #
    # The learner's own first name, as Telegram signed it. Stored ONLY so other learners can
    # see who they are competing with — the one place in this product where one user's
    # personal data is shown to another, which is why it is worth a comment rather than a
    # column definition.
    #
    # Refreshed on every visit rather than written once: a name is something people change,
    # and a stale one is being shown to strangers.
    display_name: Mapped[str | None] = mapped_column(Text, default=None)

    # Opt OUT, not opt in. A leaderboard nobody has opted into is an empty screen, and a
    # Telegram first name is already visible to anyone sharing a group with them. The switch
    # is what makes it defensible: someone who does not want to appear must be able to say
    # so in one tap and never appear again — including retroactively.
    leaderboard_opt_out: Mapped[bool] = mapped_column(default=False)

    # Stop the come-back nudges. Separate from the leaderboard switch on purpose: one is
    # about being SEEN by other learners, the other about being CONTACTED, and somebody who
    # wants their name off a scoreboard has not asked to stop hearing from the product.
    #
    # Without it the only way to stop a reminder was to block the bot — which also stops
    # the payment notices and the "your access ends Friday" warning, so the cost of one
    # unwanted message was every wanted one.
    reminders_off: Mapped[bool] = mapped_column(default=False, server_default="0")

    # Streak freezes held. A freeze covers ONE missed day, so an evening that got away from
    # someone does not erase a month of work — which is the single most demoralising thing a
    # streak can do, and why people abandon them rather than start again.
    #
    # A balance rather than a flag: earned slowly, spent automatically, capped. Which DAYS
    # were covered lives in the event log, not here — see api/services/streak.py.
    streak_freezes: Mapped[int] = mapped_column(default=0)

    onboarded_at: Mapped[datetime | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    progress: Mapped[list[Progress]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Progress(Base):
    """Leitner state, one row per (user, question) actually seen."""

    __tablename__ = "progress"
    __table_args__ = (
        # The hot path: "what is due for this user right now".
        Index("ix_progress_due", "chat_id", "due_at"),
    )

    chat_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.chat_id", ondelete="CASCADE"), primary_key=True
    )
    question_id: Mapped[int] = mapped_column(ForeignKey("questions.id"), primary_key=True)

    box: Mapped[int] = mapped_column(default=1)
    due_at: Mapped[datetime] = mapped_column(default=utcnow)
    seen: Mapped[int] = mapped_column(default=0)
    wrong: Mapped[int] = mapped_column(default=0)
    last_answer_at: Mapped[datetime | None] = mapped_column(default=None)

    user: Mapped[User] = relationship(back_populates="progress")


class ReferralLink(Base):
    """A `t.me/bot?start=<code>` link that grants a free trial, and nothing else does.

    Payment moved off Tribute to direct arrangement — someone messages the owner and is
    granted access by hand. That leaves the trial with no delivery mechanism, and handing
    one to everybody who taps /start gives the product away to anyone who finds the bot.

    So the trial rides on the LINK. A code posted to a specific channel, with its own
    length, is a trial the owner chose to give to a specific audience; a bare /start is
    not. `users.source` has recorded the /start payload since before this existed, so the
    attribution and the entitlement are the same fact and cannot disagree.

    `trial_days` is per link on purpose: an influencer's audience can be worth fourteen
    days where a cold channel is worth three, and that is a judgement about the audience
    rather than about the product.
    """

    __tablename__ = "referral_links"

    # The /start payload. Telegram allows 64 chars of [A-Za-z0-9_-]; `_clean_source` in
    # api/services/users.py enforces the same set on the way in, so a code that cannot be
    # typed into a link cannot be created here either.
    code: Mapped[str] = mapped_column(Text, primary_key=True)
    label: Mapped[str] = mapped_column(Text, default="")
    trial_days: Mapped[int] = mapped_column(default=7)

    # Switched off rather than deleted. A dead link must stop granting trials while the
    # users it already brought keep their `source` — deleting the row would erase the
    # attribution of everyone who arrived through it.
    active: Mapped[bool] = mapped_column(default=True)

    # Optional cap. None means unlimited; a number is what makes a link safe to post
    # somewhere public.
    max_uses: Mapped[int | None] = mapped_column(default=None)

    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    created_by: Mapped[int | None] = mapped_column(BigInteger, default=None)


class Purchase(Base):
    __tablename__ = "purchases"

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)

    # Tribute's id for the payment. UNIQUE is doing real work here: webhook
    # redelivery is normal, and without it a retried delivery would extend the
    # pass again. Refunds are matched on this too.
    tribute_purchase_id: Mapped[str] = mapped_column(Text, unique=True)

    tier: Mapped[str] = mapped_column(Text)
    amount_cents: Mapped[int]
    currency: Mapped[str] = mapped_column(Text, default="EUR")

    # What the pass was extended to when this purchase was applied. Stacking
    # extends from the current expiry, not from today, so this is not derivable
    # after the fact.
    extended_to: Mapped[datetime | None] = mapped_column(default=None)

    # What the pass expired at BEFORE this purchase. Null for rows written before this
    # existed, and for a user who had no pass at all.
    #
    # A refund used to subtract TIER_DAYS[tier] — our own idea of how long the tier lasts.
    # Subscriptions grant TRIBUTE's expires_at, which is a real billing period and not
    # exactly 30 or 90 days, so the two disagreed: revoking a 31-day month took 30 and
    # left a free day, and a short first period had two days taken that were never given.
    # Storing the previous value makes a refund exact rather than approximately fair.
    extended_from: Mapped[datetime | None] = mapped_column(default=None)

    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    refunded_at: Mapped[datetime | None] = mapped_column(default=None)


class StreakDay(Base):
    """One row per day a learner actually met the daily goal.

    WHY A TABLE, WHEN THE STREAK IS OTHERWISE DERIVED

    The old rule — "answered anything" — could be derived from the event log with one
    grouped query over dates. The new rule cannot: a day counts only once ten DISTINCT
    questions have been answered at a credited pace, and only if it began at least
    STREAK_MIN_GAP after the previous qualifying day. That is a forward walk over every
    answer a learner has ever given, and it is the same shape as the leaderboard scan the
    proposal singled out as unable to carry its own traffic: work proportional to total
    history, repeated on every profile view.

    So the expensive part is computed ONCE, at the moment it becomes true, and the read is a
    small indexed lookup of dates. Everything downstream — the count, the freeze, the
    milestone — still derives from these rows and stores nothing.

    `day` IS A ROME DATE, NOT UTC

    Stored as text, because it is a calendar day in a fixed civil timezone and not an
    instant. The distinction matters at exactly the hour it is hardest to test: with UTC the
    day rolls over at 02:00 Rome in summer, so a learner studying at 23:50 and 00:10 puts
    six questions into each of two days and fails both.

    `qualified_at` is the instant the day was earned, in UTC, and is what the minimum-gap
    rule compares against. Two columns in two different time systems is deliberate: one is a
    civil date, the other is a duration measured between two moments, and conflating them is
    how the gap rule would quietly gain or lose an hour twice a year.
    """

    __tablename__ = "streak_days"
    __table_args__ = (
        # (chat_id, day) is the primary key, so recording the same day twice is refused by
        # the database rather than by a "have I already?" query — the check and the write
        # cannot interleave, and restoring a backup cannot double up.
        PrimaryKeyConstraint("chat_id", "day"),
    )

    chat_id: Mapped[int] = mapped_column(BigInteger)
    day: Mapped[str] = mapped_column(Text)
    qualified_at: Mapped[datetime] = mapped_column(default=utcnow)
    # Distinct questions answered at the instant the day qualified. Never updated afterwards:
    # it exists to show what the day was earned with, and a column that must be kept current
    # on every subsequent answer is a write on the hot path for a number nobody reads.
    questions: Mapped[int] = mapped_column(default=0)


class LeagueSlot(Base):
    """One row per (learner, week, question): the first time they answered it this season.

    THE ONE RULE THAT CLOSES EVERY FARMING ROUTE. A point is earned per QUESTION per week,
    not per answer, and this table is what makes "per question" enforceable rather than
    aspirational. Three routes exist and all three are ordinary product features:

      · a repeat round draws from questions the learner has already got right — an unlimited
        stream of guaranteed-correct answers;
      · an exam re-serves questions they have seen;
      · practice hands a missed question back after ten minutes, by design.

    The slot is spent by the FIRST answer even when it is wrong. Deliberately: refunding it
    would make guess-then-retry the optimal play, which is precisely the behaviour a product
    built on understanding the question exists to discourage.

    `first_at` and `correct` cost nothing — both are known before the write — and together
    they make the season replayable: "why do I have 34 points" is answerable from this table
    without a `scored` column, which would need a second UPDATE per scoring answer purely for
    audit.

    WITHOUT ROWID because the whole table is its own primary key and it is only ever touched
    by that key. At ten thousand learners it is the largest table in the product.
    """

    __tablename__ = "league_slot"
    __table_args__ = (
        PrimaryKeyConstraint("chat_id", "week", "question_id"),
        {"sqlite_with_rowid": False},
    )

    # ON DELETE CASCADE, unlike `streak_days` above, and that is not a style preference.
    # `/delete` anonymises events rather than deleting them, so a table keyed on chat_id with
    # no cascade SURVIVES account deletion — the learner comes back with `/start`, the same
    # Telegram id, and a ledger saying every question is already spent.
    chat_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.chat_id", ondelete="CASCADE")
    )
    # The season, as the ISO date of its Monday. NOT strftime('%Y-%W') and NOT
    # (year, isocalendar week): the first splits Monday 2025-12-29 across two keys and the
    # second collides with it, both verified. This is the same value `leaderboard.week_start`
    # already returns, so the storage key and the API's `week_start` field cannot drift.
    week: Mapped[str] = mapped_column(Text)
    question_id: Mapped[int] = mapped_column(Integer)
    first_at: Mapped[datetime] = mapped_column(default=utcnow)
    correct: Mapped[bool] = mapped_column()


class LeagueDay(Base):
    """The two daily ceilings, in one row because they share a key and a rule.

    `scored` is what turns the daily cap into a primary-key upsert instead of a COUNT:

        INSERT ... VALUES (chat_id, day, 1)
        ON CONFLICT DO UPDATE SET scored = scored + 1 WHERE scored < <cap>

    which returns rowcount 1 while under the cap and 0 once at it. So the cap is enforced by
    the database in a single statement, the statement's own return value is the "did this
    score?" signal, and there is no read-then-write window for two concurrent answers to
    slip through.

    `exam_bonus` is the once-a-day mock-exam payment, same shape. It matters that it is
    atomic here specifically: an exam can be graded by a GET, when a deadline is discovered
    to have passed, so two requests really can arrive at the same grade together.

    The day is UTC, not Rome. A UTC week contains eight distinct Rome dates, so a Rome day
    would let the boundary Monday's cap straddle two seasons.
    """

    __tablename__ = "league_day"
    __table_args__ = (
        PrimaryKeyConstraint("chat_id", "day"),
        {"sqlite_with_rowid": False},
    )

    chat_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.chat_id", ondelete="CASCADE")
    )
    day: Mapped[str] = mapped_column(Text)
    scored: Mapped[int] = mapped_column(default=0, server_default="0")
    exam_bonus: Mapped[int] = mapped_column(default=0, server_default="0")


class LeagueScore(Base):
    """The running weekly total. Load-bearing, not an optimisation.

    The board it replaces loaded every answer event of the week into Python and decoded each
    one, on every single view — so the work was weekly-answers x viewers and grew with the
    square of the user base. Measured: about a second in raw SQLite at 2,000 active learners,
    two to four through the app, while holding one of fifteen database connections.

    Expressing the new rules as one query over the event log is worse, not better: measured
    at 1,444 ms per view at ten thousand users, because per-question deduplication has to
    scan that learner's whole week. So the total is kept as points are scored and the board
    becomes an index seek.

    `seed` IS THE TIEBREAK, AND IT IS NOT `chat_id`. Ordering ties by Telegram id hands every
    tie to the oldest account — and under a 40-a-day ceiling exact ties are the normal case,
    so the same three accounts would take the medals every week. It would also leak a total
    ordering of the ranked population by registration date to anyone who can read a score.
    A per-row random seed is stable within a season, so the board does not flicker, and
    reshuffles between seasons. Not "when the score was reached", which publishes study times.

    NO `leaderboard_opt_out` COPY HERE. It would make the count index-only and it would break
    the retroactive opt-out, which is the entire mechanism that makes showing real first names
    to strangers defensible. It stays a live join.
    """

    __tablename__ = "league_score"
    __table_args__ = (
        PrimaryKeyConstraint("chat_id", "week"),
        # Not optional. The primary key leads with chat_id, so `WHERE week = ?` cannot seek
        # and the table grows by one row per active learner per week forever. Measured at 52
        # seasons banked: 10.7 ms per view without it, 1.5 ms with — and the multiplier is
        # literally the number of weeks since launch, so it benchmarks perfectly in week one.
        Index("ix_league_score_week_points", "week", "points"),
        {"sqlite_with_rowid": False},
    )

    chat_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.chat_id", ondelete="CASCADE")
    )
    week: Mapped[str] = mapped_column(Text)
    points: Mapped[int] = mapped_column(default=0, server_default="0")
    seed: Mapped[int] = mapped_column()


class Event(Base):
    """Append-only analytics log (plan §9). Never read on the hot path."""

    __tablename__ = "events"
    __table_args__ = (
        Index("ix_event_type_time", "type", "created_at"),
        Index("ix_event_chat_time", "chat_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    type: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict | None] = mapped_column(JSON, default=None)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Report(Base):
    """"This explanation is wrong" from the in-bot report button.

    Volume per 1,000 questions served is a headline quality metric, and these are
    the first place to look when the correction rate is being estimated.
    """

    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    question_id: Mapped[int] = mapped_column(ForeignKey("questions.id"))
    cluster_id: Mapped[int | None] = mapped_column(ForeignKey("clusters.id"), default=None)
    lang: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(default=None)


class Suggestion(Base):
    """"What should we add?" — the form at the top of Settings.

    The people who know what is missing from a study app are the ones sitting the exam this
    month, and until this existed their only route was finding a support handle at the
    bottom of the same screen and composing a message to a stranger.

    Two states, `handled_at` null or not, matching the reports queue beside it. A suggestion
    is read or it is not; anything richer is a workflow nobody asked for.
    """

    __tablename__ = "suggestions"

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    text: Mapped[str] = mapped_column(Text)
    # The interface language at the time of writing. The owner reads four languages' worth
    # of these and needs to know which one a message is in before opening it.
    lang: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    handled_at: Mapped[datetime | None] = mapped_column(default=None)


class Analysis(Base):
    """One piece of AI study advice, kept so a second tap does not pay twice.

    The first AI cost in this product that scales with USERS rather than with content.
    Explanations and translations are capped by the question bank and shared by everyone;
    this one is about one learner and can never be reused. Storing it is what makes the
    cooldown enforceable by looking at what exists, rather than by a counter that can drift
    away from what was actually generated.
    """

    __tablename__ = "analyses"
    __table_args__ = (Index("ix_analysis_chat_time", "chat_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    # Recorded, but deliberately NOT part of the cooldown key — see the migration.
    lang: Mapped[str] = mapped_column(Text)
    body: Mapped[dict] = mapped_column(JSON)
    tokens_in: Mapped[int] = mapped_column(default=0)
    tokens_out: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)


class QuizSession(Base):
    """A bounded, gradeable sitting — an exam or a practice run.

    The exam paper is FROZEN at creation rather than served question by question, and
    that one decision buys four things at once:

      · no repeats. Serving one at a time via `selection.next_question` would re-serve
        the exam's own misses: box 1 is a 10-minute interval, the exam runs 20 minutes,
        and selection orders strictly by `due_at`, so a question missed at minute 2 is
        the top candidate again at minute 12.
      · resumability. The Mini App persists nothing across a reopen, so a client-held
        paper is lost the moment the user backgrounds Telegram.
      · server-side grading, with no need to trust anything the client sends back.
      · the whole paper can ship in one response, which removes thirty blocking round
        trips from a screen with a clock running on it.

    `expires_at` is computed and enforced HERE. A countdown held by the client is
    editable by anyone who can open devtools, and the Mini App's only identity is
    initData — there is no session cookie to bind a deadline to.
    """

    __tablename__ = "quiz_sessions"
    __table_args__ = (
        # "does this user have something open right now", asked on every app open.
        Index("ix_session_open", "chat_id", "state"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.chat_id", ondelete="CASCADE"), index=True
    )
    mode: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text, default="open")

    started_at: Mapped[datetime] = mapped_column(default=utcnow)
    # Null for practice, which has no clock.
    expires_at: Mapped[datetime | None] = mapped_column(default=None)
    finished_at: Mapped[datetime | None] = mapped_column(default=None)

    # The rules THIS sitting was graded under, copied at creation rather than read from
    # constants at grading time. Plan §11 leaves the format open (30 vs 40 questions),
    # so changing it later must not silently re-grade or misreport an exam already sat.
    question_count: Mapped[int] = mapped_column(default=0)
    max_errors: Mapped[int | None] = mapped_column(default=None)

    # Denormalised at finish so history and stats never re-walk the items.
    answered: Mapped[int] = mapped_column(default=0)
    wrong: Mapped[int] = mapped_column(default=0)
    passed: Mapped[bool | None] = mapped_column(default=None)

    user: Mapped[User] = relationship()
    items: Mapped[list[QuizSessionItem]] = relationship(
        back_populates="session", cascade="all, delete-orphan", order_by="QuizSessionItem.ordinal"
    )


class QuizSessionItem(Base):
    """One question on the paper, and the answer given to it.

    Keyed on (session_id, ordinal), NOT on (session_id, question_id). Practice mode has
    no fixed paper and a wrongly-answered question is *supposed* to come back within the
    same sitting — a uniqueness constraint on the question would turn that into an
    IntegrityError mid-session. Distinctness of an exam paper is enforced where it is
    actually wanted, in `selection.exam_paper`.
    """

    __tablename__ = "quiz_session_items"

    session_id: Mapped[int] = mapped_column(
        ForeignKey("quiz_sessions.id", ondelete="CASCADE"), primary_key=True
    )
    ordinal: Mapped[int] = mapped_column(primary_key=True)
    question_id: Mapped[int] = mapped_column(ForeignKey("questions.id"), index=True)

    # Null until answered. `given` is the user's Vero/Falso; `correct` is stored rather
    # than derived so a results screen never re-reads the answer key, and so a later
    # correction to the bank cannot silently rewrite a past exam result.
    given: Mapped[bool | None] = mapped_column(default=None)
    correct: Mapped[bool | None] = mapped_column(default=None)
    answered_at: Mapped[datetime | None] = mapped_column(default=None)

    session: Mapped[QuizSession] = relationship(back_populates="items")


class WebhookDelivery(Base):
    """Every Tribute delivery, kept verbatim.

    The only forensic record was a container's stdout, and today proved what that is
    worth: a redeploy replaced the container and six hours of webhook history went with
    it. When a customer says "I paid and got nothing", the question is what Tribute
    actually sent and what we actually answered — and the answer lived in a log that no
    longer existed.

    The RAW BODY is stored, not a parsed version. The HMAC covers exact bytes, so a
    re-serialised copy could not be used to re-verify a disputed delivery, and the fields
    we failed to understand are precisely the ones worth reading later.

    Written for rejected deliveries too. A signature that did not match is the most
    interesting row in the table — it is either a misconfiguration or someone probing —
    and a table that only records successes cannot tell you either.
    """

    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        Index("ix_webhook_received", "received_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    # Tribute's event name, when we could read one. Null means the body did not parse.
    name: Mapped[str | None] = mapped_column(Text, default=None)
    chat_id: Mapped[int | None] = mapped_column(BigInteger, default=None, index=True)

    # What we did: applied | duplicate | renewed | trial | cancelled | refunded |
    # rejected | unknown-user. The same string the endpoint returned.
    outcome: Mapped[str] = mapped_column(Text)

    signature_ok: Mapped[bool] = mapped_column(default=False)

    # Capped, because a body is small and an unbounded column is how one malformed
    # delivery fills a disk — which is not hypothetical on this box.
    body: Mapped[str] = mapped_column(Text)

    received_at: Mapped[datetime] = mapped_column(default=utcnow)
