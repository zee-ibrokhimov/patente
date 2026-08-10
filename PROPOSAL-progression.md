# Items 7, 8 and 9 — what to build, what to change, what not to build yet

## Your direct question first: which error rate?

**Neither all-time nor "last session". Use the last 100 answers.** All-time is what ships today and it is the wrong number — it barely moves when the learner improves, so they watch a figure that doesn't reward them. A single sitting is 30 answers and swings ±14 points on luck alone: the same learner would see 6% one evening and 34% the next. Your "last 3 sessions" instinct is right, but "session" has no fixed length in this app (practice batches extend themselves), so the window has no defined precision. 100 answers is about 3.3 exams — what you meant — and the app already uses exactly that window to compute the readiness score on the profile screen. Using the same window in both places is the only way the two screens stop contradicting each other.

Below 100 answers, show no percentage at all: `47 / 100 answers — the error rate unlocks at 100`. All-time stays as a small caption under the tile. It answers a different question ("how far have I come") and it's honest as long as it isn't the number being watched.

---

## Decisions you need to make

| # | Decision | Recommendation |
|---|---|---|
| 1 | Error-rate window | **Last 100 answers**, hidden below 100. All-time as a caption. |
| 2 | Error breakdown structure | **25 ministerial topics grouped into 7 families**, ranked by *expected mistakes per exam*, not by error rate. Your "signs in 3" goes inside the sign family, where it's actually correct. |
| 3 | Who gets the AI analysis | **Premium, plus one free taster per account** (after 100 answers and 7 days of account age). Say no if you'd rather keep it strictly paid — costs you nothing either way. |
| 4 | Does the non-AI breakdown stay free? | **Yes.** The free screen is what makes the paid button worth tapping. |
| 5 | Daily streak requirement | **10 distinct questions a day, right or wrong. It never increases.** This reverses your brief. Reasons below. |
| 6 | Freezes | **Keep automatic** (spent for the learner, not chosen). One earned per 7 days, hold up to 3. Refuse the monthly refill. |
| 7 | 14-day streak reward | **7 days of Premium, once per account ever, free users only.** Your version (every 14 days, everyone) costs €3,300–5,400/year at 10,000 users and hands free renewals to people who already pay. |
| 8 | Weekly rankings | **Build it, but do not launch it yet, and rebuild the scoring engine first.** You have 7 registered users. Also: the current design cannot survive its own traffic at 2,500 users. |
| 9 | Ranking prize | **One 14-day grant per person, ever** — not one per quarter. Honest cost of the tighter rule is about €6/person/year. And **one single global league**, never divisions. |
| 10 | Ranking pause | **Display-only.** Show last week's podium for 3 days; scoring never stops. A real 3-day gap makes your season 4 days long and punishes people for studying on Monday. |
| 11 | Order of work | **Security and rate-limit fixes → error analysis → streaks → rankings.** And self-serve checkout before any of them. |

---

## Before any of this: four holes that make the features unsafe

Two of these three features hand out real product for free. Right now the app cannot tell a learner from a script. These are not footnotes; they change what the features are.

### 1. The app gives away the exam answer key for free

`api/services/quiz_sessions.py` line 426 returns all 30 items of a submitted exam — including questions never answered — and every item carries the ministerial correct answer. The file's own comment right above that line explains the danger and then the code does it anyway for exams.

Two HTTP requests get 30 answer keys with no questions answered and no record written. About 2,000 requests — a few minutes of scripting — covers 98.6% of your 7,106-question bank. After that, anyone passes any exam at will.

This kills the ranking design's centrepiece. The plan was "+40 points for passing a mock exam", justified by the fact that passing 30 questions by guessing is a 1-in-240,000 event. With the key scraped, it's a 1-in-1 event, and the bonus designed to put honest learners above grinders hands grinders a free 40 points a day.

**Fix:** return the correct answer only for questions the learner actually answered. Half a day of work.

### 2. Answers can be posted with no quiz, no timing, no limit

`POST /webapp/answers` accepts any question id, any number of times, with no session, no pacing rule and no rate limit. Every threshold in these three designs — 10 questions a day for the streak, 100 answers to unlock the AI analysis, 20 points to be ranked — is therefore a count of HTTP requests, not a study signal. 100 answers takes about 30 seconds to fake.

**Fix:** rate-limit the answer endpoint (roughly 1 answer per 1.5 seconds, 500 a day per account, rejected outright rather than silently ignored) and only count answers toward streaks and points when they come from a real sitting.

### 3. "Once per learner ever" is not true — /delete resets everything

`api/services/users.py` line 163: deleting your account strips your ID from the whole event history. Two taps in the bot, then `/start` and you're new again. Every "once ever" guarantee in all three designs is derived from that history, so all of them reset: the streak Premium reward, the free AI taster, the ranking win count, the referral trial cap.

Earn a free week, use it, delete, restart, repeat — every 14 days, forever, for about 20 requests a day.

**Fix:** a small `grants` table keyed on Telegram ID that records only "this account was given X free days on this date" and survives deletion. Keeping a record of free product given is defensible retention; the alternative is an unbounded giveaway. Also make it a database uniqueness rule rather than a "has this happened before?" query — otherwise restoring last night's backup re-issues every grant.

### 4. The leaderboard cannot carry its own traffic

The current scoring code loads every answer event of the week into memory and decodes each one, on every single view of the Ratings tab. So the work is *weekly answers × number of viewers* — it grows with the square of your user base.

Measured: 400,000 weekly answer rows (≈2,000 active learners) takes about 1 second in raw SQLite and 2–4 seconds through the app's normal path, holding one of only 15 database connections the whole time. At 10,000 users that's 5–11 CPU-hours a week of pure leaderboard recomputation, and the app falls over long before the AI bill is noticeable.

The proposal said this was "years away". It isn't — 100,000 answers a week is roughly 2,500 registered users.

**Fix:** keep a running weekly score per learner, updated when a point is scored, so showing the board is one small lookup instead of a full scan. The de-duplication rule below actually makes this easier to build, not harder. Do this *before* rankings launch, not after.

---

## Item 7 — Error analysis

### The breakdown

Your memory of "road signs divided in 3" is real but narrower than you think. The highway code divides *vertical* signs into three: danger, prescription (prohibition, obligation, right-of-way) and information. That's 2,436 questions. But another 950 sign questions — road markings, traffic lights, police signals, supplementary panels, roadworks — aren't vertical signs and would have nowhere to go in a 3-way split.

So: keep the ministry's 25 topics, which are already stamped on every question and are what every Italian study book uses, and group them into 7 families. Your three-way split sits inside family 1, where it's correct.

| Family | Questions | Share of bank | Questions per 30-question exam |
|---|---|---|---|
| Vertical signs (danger / prescription / information) | 2,436 | 34.3% | 10.3 |
| Rules of circulation | 1,724 | 24.3% | 7.3 |
| Other signage — markings, lights, panels, roadworks | 950 | 13.4% | 4.0 |
| The vehicle and the environment | 696 | 9.8% | 2.9 |
| Definitions and driver duties | 531 | 7.5% | 2.2 |
| Safety, distances, driver condition | 401 | 5.6% | 1.7 |
| Documents, penalties, insurance | 368 | 5.2% | 1.6 |

These are exact, not estimates: the exam draws uniformly from the whole bank, so a topic's share of the bank *is* its share of the exam.

### Rank by lost marks, not by error rate

This is the part that makes the screen worth building. Today's stats screen sorts topics by error rate, which puts a topic where the learner answered 3 questions and missed 2 (67%) above one where they answered 300 and missed 120 (40%). The first is noise. The second is where every lost mark actually lives.

Instead: multiply each topic's error rate by how often it appears on an exam, and rank by that. Then the headline of the screen is one sentence a learner can act on: **"On a 30-question exam you'd average 4.2 mistakes. You pass at 3."**

| Rule | Value | Why |
|---|---|---|
| Headline window | Last 100 answers, within 90 days | Same window as readiness; ~3.3 exams |
| Hide headline below | 100 answers | A percentage on 12 answers is a guess told to someone deciding whether to book a paid exam |
| "Improving" arrow shows only at | 10 percentage points | Below that, the change is statistically indistinguishable from luck, and a flickering arrow teaches people the number is meaningless |
| Topic breakdown window | 90 days, counting only the most recent answer per question | Today's per-topic number is a lifetime tally: a question you missed four times last month and fixed today drags the topic down forever, with no action that clears it |
| Topics with under 10 questions answered | Shown in a greyed "Not tested yet" band, no percentage | Below 10, the margin of error is wider than the entire useful range of the metric |
| Coverage shown on every row | Always | 0% errors on 12 of 662 information-sign questions is not mastery, and the ranking would otherwise show it as your best topic |

**Cold start looks like a bug.** Most users on day one see no percentage and a "Not tested yet" band containing nearly every topic. That's the truthful state, but it reads as an empty feature. The `47 / 100` progress line and the coverage bars are load-bearing — without them this ships as a blank screen.

**One honest caveat:** practice deliberately re-serves what you got wrong, so measured topic error rates are pessimistic compared to a fresh exam. Label the prediction as "on the questions you've met" and treat it as an upper bound. Don't print it as a plain exam prediction.

### The AI button

It sits inside the breakdown screen, exactly as you described. It never gates the screen: if the model is slow or fails, the deterministic top-3 renders instead, at zero cost.

What gets sent: no name, no Telegram ID, no chat ID. The topic table, plus 30 of the learner's own recent wrong answers spread across their five worst topics. That sample is the entire difference between specific advice and a horoscope.

What comes back is structured, not a wall of text: a two-sentence summary, three focus areas each with a concrete action, one cross-topic habit, and **a suggested next sitting with a button that starts it**. That last part is the point of the feature. Advice a learner can't act on in one tap is advice they won't take.

Two hard rules on the model output:
- **No numbers in the prose.** Every figure on that screen is computed by us. A model that writes "your error rate is 31%" when we computed 24% breaks the product's core promise.
- **It may not teach traffic law.** It says where and how to study, never what a rule is. Rules are the explanations feature's job and those are grounded in the actual statute. An ungrounded model inventing a speed limit is precisely the failure the existing explanation system was built to prevent.

### What it costs

| | Estimate |
|---|---|
| Cost per analysis | $0.004 optimistic, **budget $0.012** |
| Why the higher number | The one real measurement in your own database is 3,353 output tokens, and STATUS.md section 12 measured this model burning 9,273 where an older one used 1,127. The cheap estimate is 2–6× low. Run `content/measure_costs.py` against the real prompt before shipping — it exists for this. |
| Cooldown | **48 hours, checked before anything else, and the same regardless of language** |
| At 100 users | $0.03–0.17/month |
| At 1,000 users | $0.34–1.68/month |
| At 10,000 users | $3.40–16.80/month ($40–200/year, against roughly €6,600/year of subscription revenue) |

The AI bill is not your problem at any scale you asked about. But one detail matters: **the cooldown must be checked first, before the language check.** As originally written, switching UI language counted as a "new" request, so four taps in Settings buy four analyses inside one cooldown — and if the language path skips the cooldown entirely, one account can generate roughly 360 calls an hour, $34–166 a day, on a €2.99 subscription. Check "has this account had an analysis in 48 hours?" first, always, and treat language as a detail underneath that. Add a hard ceiling of 20 analyses per account per 30 days as a backstop.

One more warning from your own codebase: `translations.py` documents that this exact model rejects a parameter, and the retry silently dropped *all* settings — "the constant was set, the tests passed, and production never once ran a low-effort translation." That bug cost 5–10× on speed and money and nobody noticed. Copy that file's retry ladder rather than writing a fresh call, and log the token count on every analysis so a silent revert is visible.

**Also worth knowing about the shape of this cost, not its size.** Every AI expense you have today is capped by content and cached forever — 3,382 explanations, 7,106 translations, ceiling €70–100 total, and the marginal cost of one more user is zero. The per-learner analysis is the first AI cost in this product that scales with *users* and can never be shared between them, because it's about them. It's small at every scale you asked about, but it is the first line in your costs with no ceiling, and the 48-hour cooldown is the only thing setting its slope. Treat that number as a pricing decision, not a technical one.

---

## Item 8 — Streaks: where your brief backfires

You asked me to say this plainly, so:

### Counting *correct* answers is the mistake, not the daily habit

Measured accuracy across your whole event log is 123 of 223 — **55%**. So:

| Your rule | What it actually costs the learner |
|---|---|
| 10 correct | 18 answers |
| 15 correct (7-day streak) | 27 answers |
| 20 correct (14-day streak) | **36 answers — longer than a full exam, every day, forever** |

And it's backwards: at 85% accuracy a strong learner does 24 questions to reach 20 correct; a struggling learner does 40. **The person who needs the streak most pays double for it.**

There's a deeper problem. This app teaches by spaced repetition — a wrong answer is what *schedules the review*. It is worth more than a right one. Telling learners that getting things wrong doesn't count contradicts the entire method the product is built on. Performance belongs in the readiness score and the leaderboard. The streak should measure **showing up**.

### The escalating ladder guarantees every streak ends

A requirement that rises forever against time that doesn't rise guarantees termination. That's not a motivation mechanic, it's a countdown. Duolingo's daily goal does not increase with streak length either — the *rewards* escalate, not the requirement.

It also exhausts your content. The bank is 7,106 questions. Your day-15 requirement (about 31 answers a day) walks the entire bank in **229 days**, after which the daily goal is forcing pure repeats. Any fixed goal above ~19 questions a day is arithmetically unsustainable against the material you have.

And practically: a rising bar makes the streak code enormously more expensive, because whether today counts starts depending on what happened last week, tangled up with freezes. Today it's a simple, testable rule. Ask what happens if a freeze covered day 9 — does the ladder advance? There is no honest answer.

### What to build instead

| Rule | Value | Why |
|---|---|---|
| Daily goal | **10 distinct questions, right or wrong** | Measured pace in your own data: median 7.4 seconds between answers, so 10 questions is **74 seconds**; 2.6 minutes for a slower learner; 6.3 minutes for someone reading translations and explanations. Doable on a bus on a bad day. |
| Escalation | **None. 10 forever.** | Three reasons above. |
| Same question counted twice | No — distinct questions only | Already happening in your data: one learner logged 83 answers over 68 distinct questions in a day. Spaced repetition re-serves a missed question after 10 minutes, so without this the goal is a 60-second loop. |
| Minimum gap between qualifying days | **8 hours** | Without it: answer 10 questions at 23:58, the same 10 at 00:01, and you've banked two days. One four-minute burst every 48 hours keeps a streak alive forever and pays out free Premium. This rule is not optional. |
| Minimum pace | 2.5 seconds between answers, for goal purposes | 10 questions at that pace is 25 seconds. No burden on a real learner; fatal to a script. |
| Day boundary | **Rome time, not UTC** | Currently the day rolls over at 02:00 Rome in summer. A learner doing 6 questions at 23:50 and 6 at 00:10 would fail both days. |
| Freeze earn rate | 1 per 7 days | That's 4.3 a month against the 5 you asked for. You already have almost exactly what you wanted. The 7 buys a rule people can state: "one for every week you keep going." |
| Freeze cap | Raise 2 → **3** | The old cap was set when a day cost one answer. A day now costs 10 questions. Safe because a freeze can only ever bridge *yesterday* — holding 3 buys three separate single misses, never a three-day absence. |
| Monthly refill | **Refuse** | It hands protection to someone who has never built a streak, and it needs a monthly scheduled job that this system doesn't have. Earning needs no job at all. |
| Manual freeze use | **Refuse** | You asked for "so they can freeze if they missed." A freeze the learner has to remember to use fails at exactly the moment it was for — they've already missed the day. |
| Big-day marker | 30 questions = a "full round" badge on the calendar | Purely cosmetic. Rewards a big day without making tomorrow harder. |
| At-risk nudge | 19:00 Rome, once a day, only for streaks of 3+ | Nobody gets nagged about a streak they haven't invested in yet. |

Net effect over three weeks: the requirement gets *harder*, not softer. Today it's "open the app on 21 of 21 days." This is "do a 10-question round on 18 of 21 days."

**Warn yourself in advance:** the number of users holding a streak will fall, possibly sharply, when you move from "answered anything" to "10 questions". That's the bar doing its job. Don't read it as a regression and revert it.

### The 14-day reward

7 days of Premium. **Once per account ever, and only if they aren't already Premium.**

Your version — every 14 days, no exclusion — costs this:

| Version | Cost at 10,000 users |
|---|---|
| Your brief (recurring, everyone) | 3% sustaining it year-round = 300 people × 26 grants × 7 days = **€3,300–5,400/year of list value** |
| Once ever, free users only | ~5% reach it = 500 grants = **€350/year list value**, real displaced revenue well under €35 |

The worse half is the "everyone" part. Granting Premium to someone who already has a paid subscription *extends their expiry* — so a paying subscriber who keeps a streak never renews. You'd be paying your best customers to stop buying. Every subsequent 14-day milestone should pay a profile badge instead, reusing the same cosmetic system the rankings need anyway.

---

## Item 9 — Weekly rankings

### Do not launch this yet

Production has 7 users, 4 of whom have ever answered a question, and 223 answers in total. The board today would rank two people. Build it behind the existing 5-player threshold and let it switch itself on when the population arrives. Hide the Ratings tab entirely until then — a tab that says "it's quiet" every week for months teaches people the feature is dead; lighting it up the first week it qualifies makes its arrival an event.

### The points formula

**1 point per question you answer correctly, counted once per question per week. Plus 40 for passing a mock exam, once a day.**

| Rule | Value | Why |
|---|---|---|
| One scoring slot per question per week | First answer only; a wrong first answer spends the slot | Closes all three farming routes at once: the "repeat what you got right" mode is an unlimited stream of guaranteed-correct answers today; exams re-serve questions you've seen; practice hands back a missed question 10 minutes later. Spending the slot on a wrong answer is deliberate — otherwise guessing and retrying is the optimal play. |
| Derived from the event log, not from progress | — | Resetting progress is a button in the app. An event-based rule survives a mid-week reset; a progress-based one doesn't. |
| Cost to an honest learner | **18–22%** of their raw count | Measured on your one genuine user: 36 distinct of 46 answers one week, 68 of 83 the next. |
| Minimum pace | 2.5 seconds | Measured: exactly 1 of ~129 genuine answer gaps falls below it (0.8%). The scripted run on the same database had 64 of 78 gaps under 2 seconds. |
| Daily cap | 40 scoring answers | Four times the streak goal, so no honest learner hits it casually. Heaviest genuine day observed was 68 distinct questions, so a hard crammer loses the top of one day. Daily, not weekly, so a capped learner is fresh tomorrow rather than being told to stop studying. |
| Exam pass bonus | 40 points, once a day | Sized so an honest ceiling (~560/week) beats a grinder's ceiling (~280/week). **This only works if you close the answer-key leak first** — otherwise the bonus is 40 free points a day for the exact person it was designed to exclude. |
| Minimum to hold a rank | 20 points | Two days of the streak goal. Today one correct answer occupies a rank, which is absurd once ranks carry badges and trigger notifications. |
| Minimum for a season to award prizes | 10 ranked learners | Three of ten is a promotion zone; three of five is a participation trophy. |

Deliberately **not** included: no penalty for wrong answers (it teaches people to avoid hard topics near the week's end); no bonus for new questions (a beginner has 7,106 unseen and a loyal learner has none — it would systematically favour newcomers); no difficulty weighting (the data can't support it and it makes the rules page unexplainable); no Premium multiplier (the prize *is* Premium — that's a closed loop).

**The rule that decides support load:** three separate rules can silently make a correct answer worth zero points. If the results screen doesn't say **"+18 points · 12 questions already counted this week"** at the moment it happens, every confused learner writes to you instead. Ship that line with the formula, not after it.

### The 3-day pause — do it, but only on screen

Your literal version breaks scoring. Monday-to-Wednesday off means the season is a long weekend, and points don't stop being earned during a pause: either Monday's study counts for nothing (punishing someone for studying, on an app that simultaneously demands a daily streak), or it counts toward next season — in which case the season already started and the pause was only ever a screen.

So: the season stays Monday to Sunday with no gap, and for the first three days of the new week the Ratings screen shows **last week's finished podium as the main card**, with the live season as a line underneath it. That's the payoff screen — the only moment in the entire product where a learner is told they *finished* something. On Thursday it collapses to a one-line "Last week: winner" strip.

### Badges

Medal icons for the top 3, drawn as vector icons so they theme correctly in dark mode — this is the one place I'd push back on "icons or images". Images would need four files, a server path, and would look wrong in dark mode.

**Only the immediately preceding season's medal appears next to a name on the board** — which is exactly what you asked for ("visible in next rankings"). A stack of six medals beside one name recreates an all-time leaderboard, which is a screen that tells everyone else they've already lost. Full history lives on the learner's own profile, which is private to them.

**One thing to fix in the design:** the display name comes from Telegram and is unfiltered. Someone renames themselves "🥇 Aziz" — free, one tap, zero learning — and appears on everyone's board wearing a medal they never won. The same field accepts "Admin", a URL, or text-direction attacks, and this is the only screen in the product where one learner's data is shown to strangers. Put the medal in its own column, never next to the name, and strip emoji and control characters from names on the way in.

### The prize

| Rule | Recommendation | Why |
|---|---|---|
| Wins needed | 3 consecutive **awarded** seasons at 1st place | Your number. "Awarded" does the work: a quiet week under 10 ranked learners neither counts nor breaks the run. Without it, at today's 7 users your most active learner wins three weeks running and holds free Premium permanently. |
| Prize | 14 days of Premium | Your number. Costs €0.85–1.40 of list price. |
| Frequency | **Once per person, ever** | Not once per quarter. See below. |
| Places | 1st only; 2nd and 3rd get the badge | Extending Premium to the podium triples the giveaway and removes the reason to chase 1st. |

Here's the thing nobody costed correctly. **The league prize is not a revenue risk — it's about €24 a year, globally, at any user count.** There is one winner a week, so at most 17 grants a year exist in the whole world. The 90-day cooldown everyone worried about is protecting €24.

But that figure holds **only while there is exactly one global league.** "Duolingo format" is in your own brief, and Duolingo has divisions of ~30 people. If you ever split the league into divisions, at 10,000 users you'd have 333 leagues × 17 grants = ~5,661 grants a year ≈ **€7,900/year**. Write "single global league" into the design as a costed constraint so nobody adds divisions without redoing this number.

Given the prize is worth €24/year globally, "one grant per person ever" costs you about €6 a year in generosity and removes the whole category of abuse. Take the tighter rule.

### The "someone passed you" notification

This is the first unsolicited message this product would ever send, and the cost of getting it wrong is precise: a learner who mutes the bot over a ranking nudge also stops receiving "your Premium ends Friday" — the message the business runs on.

It fires only when **all** of these are true:

| Condition | Value |
|---|---|
| They lost 1st place, or lost the top 3 | Not any rank change — below the podium nothing is at stake |
| It's the last 48 hours of the season | Biggest volume reducer. "You lost 3rd place" on a Tuesday misrepresents the stakes; the position will change twenty times |
| The gap is under 40 points | One day's cap. Telling someone 200 points adrift that they lost their place asks for something they can't do |
| They held the place at least 6 hours | Two people trading places don't ping each other all evening |
| Max 2 per season, 12 hours apart | A third overtake in a week isn't news, it's nagging |
| Quiet 21:00–09:00 UTC | Held and sent at 09:00, which is 11:00 in Italy |
| Every message has a one-tap "turn these off" | Not a settings path. A button in the message. |

**Never name the overtaker.** "Someone passed you" is equally motivating and doesn't personalise a rivalry.

Two abuse notes worth knowing: two throwaway accounts can deliberately push a specific learner off the podium twice a season purely to annoy them into muting your bot, and the leaderboard endpoint has no rate limit, so polling it reconstructs every ranked learner's study hours and cadence. Both are cheap to fix (require the overtaking points to come from a real sitting; cache the board for 60 seconds).

### A rules page

Four cards with an icon each — reached from an info button in the Ratings header, not buried in Settings, because the people who need it are looking at the board right now:

1. **The season.** Monday to Sunday, same clock for everyone.
2. **Points.** 1 per question you get right. Each question counts once a week however many times you meet it. Wrong answers cost nothing. Max 40 a day. Plus 40 for passing a mock exam, once a day.
3. **Prizes.** Top 3 get a medal shown beside their name all next season. Win three seasons in a row and get 2 weeks of Premium.
4. **Who sees you.** Your first name and your score. Nothing else, ever. One tap to leave.

Plus a "Why didn't my points go up?" section — that's the part that actually prevents support messages.

**One thing to close:** the "hide me from rankings" switch is instant and retroactive. As designed, the winner of a season could hand the win to second place by flipping it on Sunday night and off on Monday — or freeze themselves out of a loss. Make the switch take effect from the next season, or lock it for the last 48 hours.

---

## Costs, honestly

### Money

| Item | 100 users | 1,000 users | 10,000 users |
|---|---|---|---|
| AI analysis | $0.03–0.17/mo | $0.34–1.68/mo | $3.40–16.80/mo |
| Free AI tasters (one-off) | ~$0.40 | ~$4 | ~$40–190 |
| Streak Premium (recommended: once ever, free users only) | €3.50/yr | €35/yr | €350/yr list, well under €35 real |
| Streak Premium (**your brief as written**) | €33–54/yr | €330–540/yr | **€3,300–5,400/yr** |
| League Premium (single global league) | €24/yr | €24/yr | €24/yr |
| League Premium (if you ever add divisions) | €24/yr | ~€800/yr | **~€7,900/yr** |

The context that reframes all of it: **there is currently no way for anyone to pay you.** Checkout is switched off in config; payment is a Telegram conversation with you personally. Lifetime revenue is zero and your one purchase record is for €0. So every euro figure above is a projection.

The structural asymmetry matters more than the amounts: **free Premium is granted automatically by a scheduled job; paid Premium requires you to personally answer a DM.** At 10,000 users the streak reward alone fires ~500 automatic grants while your sales path scales with your inbox. Two consequences: (a) shipping self-serve checkout is more valuable than any of items 7, 8 or 9; (b) every automatic grant message should end in a link to buy — it's the best-timed sales moment you will ever get.

### Build effort

These are my estimates for a competent developer working on this codebase, including tests, four-language copy, and the front end.

| Work | Effort | Note |
|---|---|---|
| **Security and limits** — close the answer-key leak, rate-limit answering, grants ledger that survives deletion, session-creation floor | **3–5 days** | Nothing else should ship first. About a day of it is the leak alone. |
| **Error breakdown, no AI** — 7 families, expected-mistakes ranking, coverage bars, the 100-answer window shared with the profile screen | **4–6 days** | Delivers real value on its own. |
| **AI analysis layer** | **3–5 days** | Half a day of it is running the cost measurement first. |
| **Streaks** — day goal, Rome day boundary, 8-hour rule, freeze cap, reward with the grants ledger, evening nudge, backfill for existing users | **4–6 days** | |
| **Rankings** — rebuilt scoring engine, season sealing, badges, notifications, rules page, name sanitising, ~16 new strings × 4 languages | **10–15 days** | Doubles if the scoring engine isn't rebuilt first, because you'll build it twice. |
| **Self-serve checkout** | not scoped here | Higher priority than all of the above. |

Two ongoing risks that aren't in the table. Your event log grows without a retention policy: at 10,000 users that's ~2.7 GB a year on SQLite, being scanned by streaks, the leaderboard, the profile and now the error breakdown. Decide the pruning rule *before* you build three more features that read it, and plan the move off SQLite. And every past four-language sweep on this repo has broken at least one of the copy tests — budget a day for that on each feature, not an afternoon.

---

## Build order

**Stage 0 — this week, before anything on your list (3–5 days).**
Close the exam answer-key leak. Rate-limit the answer endpoint. Add the grants ledger that survives account deletion. Fix the referral counter that goes *down* when someone deletes themselves. None of this is visible to users, and all three of your features are unsafe without it.

**Stage 1 — first shippable feature (4–6 days). Ship this next.**
The error breakdown, free, no AI. Change the headline to the last-100-answers window, add the 7 families, rank by expected mistakes per exam, show coverage. This is the highest value-per-day item on the list: it works for every user, it costs nothing to run, it needs no population to be useful, and it's the screen that makes the paid button worth tapping later.

**Stage 2 (3–5 days).**
The AI analysis on top of it. Measure the real token cost first. Cooldown checked before language, always. One free taster, then Premium.

**Stage 3 (4–6 days).**
Streaks: 10 distinct questions a day, no escalation, freezes as they already work with the cap raised to 3, the 7-day Premium reward once per account. Backfill existing users' streaks from the event log so nobody's counter resets on deploy.

**Stage 4 — build, don't launch (10–15 days).**
Rankings. Rebuild the scoring engine to keep a running weekly total before writing a single point rule. Then the formula, sealing, badges, notifications and the rules page. Leave the tab hidden until 5 learners are ranked in a real week and let it appear by itself.

**In parallel, and more urgent than any of it:** a way for someone to pay you without messaging you.

One thing you should not build at all: **divisions**. The single global league is what keeps the ranking prize at €24 a year instead of €7,900.