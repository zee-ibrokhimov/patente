# Patente Quiz Bot — Project Plan

**Status:** v2 — pricing, languages and tier split decided
**Owner:** Zee
**Last updated:** 29 July 2026

---

## 1. Product definition

A Telegram bot that drills the official Italian driving-theory question bank
(patente AM/B), showing each question in exact ministerial Italian with an
optional translation underneath, and giving the reasoning behind the correct
answer after the user replies.

**Positioning:** the Italian quiz-app market is saturated and free. The
defensible angle is *not* "another quiz app" — it is **exam-language questions
with native-language understanding**, aimed at the large population of
non-Italian speakers who are legally required to sit this exam in Italian,
French or German and currently study rules they only half-understand.

**Target user:** foreign residents in Italy preparing for patente B, who speak
Russian / English / Ukrainian / Arabic / Spanish better than Italian.

**Core loop:**

```
question (IT, ministerial wording)
  └─ translation (toggleable, user's language)
     └─ user answers VERO / FALSO
        └─ verdict + explanation of why
           └─ next question
```

---

## 2. Scope

### MVP (must ship)

- Question delivery with Vero/Falso inline buttons
- Onboarding: language choice, expectation setting, first question inside 60
  seconds — no wall of text before the user has answered anything
- Translation toggle, on/off, persisted per user
- Explanation shown after answering
- Spaced repetition (Leitner) so wrong answers return
- Per-topic error statistics
- Free tier + paid tier with paywall
- Payment, subscription state, expiry handling
- `/delete` (GDPR), privacy policy, "not an official ministry product" disclaimer
- Bad-explanation report button

### V2 (after paying users exist)

- Exam simulation: 30 questions, 20 min, fail at 4 errors
- Daily reminder / streak
- Additional languages
- Weak-topic targeted drills

### Explicitly out of scope

- Free-text Q&A or "explain differently" — turns fixed cost into per-message
  cost and is how bots go bankrupt
- Practical-exam (guida) content

---

## 3. Content pipeline — the long pole

This is 70% of the work. Everything else is a few weeks of engineering.

### 3.1 Questions

| Step | Detail |
|---|---|
| Source | Official AB listato PDF, ilportaledellautomobilista.it |
| First action | Search GitHub for an existing clean extraction before parsing |
| Extract | Text + embedded figures, preserving topic grouping and V/F key |
| **Gate** | Hand-verify 30 random questions against the PDF. A one-row misalignment between statement and answer key is silent and poisons everything downstream. |

Sign-recognition questions are a large share of the bank and are meaningless as
text — figures must be extracted and stored.

### 3.2 Translations

- Machine-translate with an LLM, not a generic translation API — legal register
  matters and context (topic + stem) improves output significantly.
- **Do not translate the ministerial wording loosely.** Users are training to
  recognise exact phrasing. The translation is a comprehension aid shown
  *underneath*, never a replacement.
- Review pass needed per language. Cheaper than explanation review because
  errors are more obvious.

**✅ Decided: Russian and English at launch, more languages later.**

Because more languages are coming, the schema must be language-agnostic from
day one:

```
questions      (id, topic, stem_it, statement_it, answer, image)
translations   (question_id, lang, stem, statement)
explanations   (question_id, lang, text, reviewed_at)
users          (chat_id, lang, translations_on, pass_expires_at)
```

Never `translation_ru` / `translation_en` as columns on `questions` — that is
the decision that costs a painful migration at language three.

### 3.3 Explanations

No official explanations exist. The Ministry publishes statements and an answer
key, nothing more.

**Cannot use:** autoscuola manuals (Egaf, Vega, Il Fiaccolaio) or commercial
quiz sites — all copyrighted.

**Can use:** the Codice della Strada (D.Lgs. 285/1992) and its Regolamento
(DPR 495/1992), freely published on Normattiva.

**Method — grounded generation:**

1. Pull CdS + Regolamento, split by article
2. Hand-map the 25 ministerial topics → relevant articles (~1 afternoon)
3. **Deduplicate first.** A large share of the bank is the same rule reworded.
   Cluster near-identical statements and write one explanation per *rule*, not
   per statement. Cuts volume dramatically and means a fix lands in one place.
4. Generate per cluster: statement + correct answer + mapped article text →
   two sentences citing the article
5. Generate once, offline, store in DB. Never at runtime.

**Authoring order matters with two languages.** Write and review the canonical
Italian explanation first, then translate the *approved* Italian into RU and EN.
One review of substance, then a lighter review of translation fidelity.
Generating RU and EN independently from the article text means reviewing the
same legal reasoning twice and getting two subtly different answers.

**Quality gates (in order of value):**

- Flag every explanation containing a number or unit (km/h, m, g/l, anni) →
  human review. Numeric claims are where models drift.
- Flag any explanation that argues for the opposite of the stored answer →
  catches bad explanations *and* leftover parsing errors from 3.1
- In-bot report button; you will be the first heavy user

**Expert review — worth paying for.** Before public launch, pay a working
autoscuola istruttore to review ~100 explanations across topics. A few hundred
euros buys you a real error rate from someone who teaches this daily, catches
the class of mistake you and the model share, and gives you a credibility line
("reviewed by a licensed instructor") that no competitor bothers with.

**Release rule:** a topic goes live only when 100% of its explanations have been
read by a human. Gate by topic, not all-or-nothing — ship in weeks, not months.
An empty explanation is acceptable; a confidently wrong one about a speed limit
is not.

---

## 4. Monetization

### 4.1 Payment rails — Tribute

Telegram requires digital goods sold *natively* by bots to use Stars (XTR),
which is expensive: on mobile the app-store cut eats roughly 30%.

**✅ Decided: Tribute (tribute.tg) as the payment layer.**

How it fits a bot paywall:

- Sell a **digital product** in Tribute; on payment, Tribute POSTs a webhook to
  your server containing the buyer's **Telegram ID**
- Your handler verifies the `trbt-signature` HMAC-SHA256 header, then writes
  `pass_expires_at` for that chat ID
- Handler must be **idempotent** — reprocessing the same payment must not
  double-extend a pass
- A `digital_product_refunded` webhook fires on refunds (App Store, Google Play
  or bank); match on `purchase_id` and revoke access. Build this from day one —
  it also implements the EU withdrawal right.

Payment methods available to the buyer: **Stars in-app, bank card via web link,
and crypto (USDT / BTC / TON)**.

Economics: **flat 10% commission**, no monthly fee. On a €6.99 pass that's
~€6.29 net via card, versus ~€4.90 through raw Stars on mobile. **Steer buyers
to the card link, not Stars** — it's worth roughly €1.40 per sale.

Frictions:

| Item | Detail |
|---|---|
| Payout schedule | Twice monthly, 10th and 25th |
| Payout minimum | €100 to bank card — ~15 pass sales before the first withdrawal |
| Processing | Days to a month depending on country and method |
| Custody | Custodial — Tribute holds funds between payouts |
| Onboarding | Creator verification required before taking payments |
| Doc conflict | Main site says EUR payouts to card; the digital-products API page says USDT with card "coming later". Confirm which applies to your account before building. |

❗ **Action item #1: ask Tribute support, in writing, whether they act as
merchant of record and handle EU VAT.** If yes, your Italian fiscal setup gets
dramatically simpler. If no, the partita IVA conversation stands. This is the
single highest-value question to resolve at M0.

### 4.2 The pricing problem nobody mentions

**This product has structural churn by design.** Users leave when they pass the
exam. Typical preparation is 1–3 months. Unlike normal SaaS, you cannot build
LTV through retention — every single customer is guaranteed to leave.

At €3/month with an average life of ~2 months, LTV ≈ €6 gross, maybe €4 net.
That has to cover acquisition cost, and paid acquisition at that LTV is very
hard to make work.

**✅ Decided: two tiers — €2.99 for 1 month, €6.99 for 3 months.**

| Tier | Price | Per month | Net after 10% |
|---|---|---|---|
| 1 month | €2.99 | €2.99 | ~€2.69 |
| 3 months | €6.99 | €2.33 | ~€6.29 |

The 3-month saves €1.98 against buying three single months — a 22% discount.
That's enough to be visible but not enough to be obvious, so **presentation
does the work**: show "€2.33/month" on the 3-month option and make it the
highlighted default.

**Both tiers must be one-time purchases, not auto-renewing.** Tribute supports
a `onetime` period. Keeping it one-time preserves the main advantage of the
pass model — no subscription lifecycle, no cancellation flow, no failed-renewal
handling, and no EU auto-renewal disclosure obligations. The user simply buys
again if they need more time.

Rules to define:

| Question | Recommendation |
|---|---|
| When does access start? | At purchase. Simplest, matches expectation. |
| Stacking | Buying again while active extends from current expiry, not from today. Lets someone top up a month onto a pass. |
| At expiry | Keep all progress data; lock translations and explanations only. Re-purchase restores instantly — lapsed users are easy to win back. |
| Upgrade path | Consider offering the 3-month at a discount to someone whose 1-month is about to expire. They've proven the product works for them. |

**Watch for:** the €2.99 option may cannibalise the €6.99 rather than expand the
market — people underestimate how long preparation takes and default to the
cheaper choice. Track the split from day one. If almost everyone picks monthly
and then buys again, widen the gap (e.g. 3-month at €5.99) or add a 6-month
tier so €6.99 becomes the sensible middle option rather than the expensive one.

### 4.3 Free vs paid split

**✅ Decided:**

- **Free:** the plain question in Italian, Vero/Falso, correct answer revealed.
  Nothing else.
- **Paid:** translations, explanations, plus (recommended) exam simulation,
  stats and spaced repetition.

**Free questions should be unlimited, not capped.** Serving them costs nothing,
and the more questions a free user answers the more often they hit the wall that
sells the product.

**The conversion moment is the wrong answer.** A free user answers, gets
"❌ Wrong — the answer is FALSO", and wants to know why. That is exactly where
the paywall belongs:

```
❌ Wrong — the answer is FALSO
🔒 Why? — unlock explanations and translations
```

Two mechanics worth building around this:

1. **Give away the first 3–5 explanations free, lifetime.** Without a taste,
   the user is asked to pay €7.99 for something whose quality they have never
   seen — and quality is the entire pitch. Let them experience a good
   explanation, then lock it.
2. **Track wrong-answers-before-purchase.** This is your core conversion metric
   and it tells you whether the free tier is too generous or too thin.

Honest risk: a free tier of "questions + right answer" is what every free
competitor already offers, so it wins no one on its own. It works only if the
locked content is visibly better than free alternatives — which puts even more
weight on explanation quality.

### 4.4 Unit economics

Fixed costs (do **not** scale with users):
- Content generation — one-time, translations + explanations
- Your time

Variable costs:
- Hosting: one small container, negligible
- Telegram/app-store cut per transaction

This is the payoff of pre-generating everything: at 10 users or 10,000, the
content bill is identical. Protect that property — any runtime LLM feature
destroys it.

---

## 5. Legal & fiscal

*Not legal advice — verify each item with a commercialista before launch.*

| Item | Note |
|---|---|
| Tax residency | Moving back to Uzbekistan means Uzbek tax residency — but only once Italian residency is properly closed (deregistration from the anagrafe). Do the formalities; residency is decided by facts, not intent. |
| Uzbekistan structure | **Worth investigating: IT Park residency.** 0% corporate income tax, 0% social tax, VAT exemptions, 7.5% employee income tax. Export-oriented companies (>50% revenue from abroad) — which this is, ~100% — get relief running to 2040. Formation takes 3–6 weeks; apply at it-park.uz. |
| ⚠️ Activity classification | From 1 April 2026, payment organisations, payment system operators and **marketplaces** lost IT Park incentives. A study-content bot shouldn't fall in that class, but verify your OKED codes against the IT Park activity list *before* registering. |
| ⚠️ Crypto payouts | **Direct purchase of goods/services with crypto is not legal in Uzbekistan as of 2026**, and crypto can only be traded on licensed UZEX-based exchanges. If Tribute pays out in USDT, receiving and converting it in Uzbekistan is constrained. Confirm Tribute can pay to an Uzbek bank card before committing. |
| EU VAT on digital services | **Does not go away by leaving the EU.** VAT on B2C digital services is owed where the customer is; non-EU sellers use the non-Union OSS scheme. This makes the Tribute merchant-of-record question *more* critical, not less — if they're MoR, it's their problem. If not, a non-EU sole trader registering for EU VAT is real overhead. |
| GDPR | Store chat ID + answer history only. No names, usernames, or message text. `/delete` command + short privacy policy linked from `/start`. |
| Content rights | Ministerial questions are public documents — attribute the source. Your explanations are yours *because* they were generated from CdS text, not a publisher's manual. |
| EU right of withdrawal | Digital content normally carries a 14-day withdrawal right unless the buyer explicitly consents to immediate access and acknowledges losing that right. With a 3-month pass this matters: capture that consent at the purchase step, or you may owe refunds on request. Verify the exact wording required. |
| Refund policy | Write one before launch, however short. Decide who issues it — you or Telegram — since Stars payouts are held ~21 days. |
| Terms of service | Needed alongside the privacy policy — what the pass grants, what happens on expiry, refund terms, acceptable use. Short is fine; absent is not. |
| Naming | Check @username availability on Telegram and the matching domain before you print anything. Avoid names implying official status ("patente ufficiale", "ministeriale") — that's both a trademark and a consumer-protection risk. |
| Disclaimer | "Study aid, not an official ministry product. Defer to your autoscuola." In `/start` and `/help`. |

❗ **Action item #2: one consultation with a commercialista before writing
payment code.** If the fiscal setup makes €3/month unviable, you want to know
now, not after building it.

---

## 6. Technical architecture

### 6.1 Two surfaces, one backend

**✅ Decided: both a Telegram Mini App and in-chat use.**

This is the right end state — but it is a second frontend, so the only way it
doesn't double the work is if neither surface owns any logic:

```
            ┌─────────────┐        ┌──────────────┐
            │  Bot (chat) │        │  Mini App    │
            │  aiogram    │        │  web frontend│
            └──────┬──────┘        └──────┬───────┘
                   │                      │
                   └────────┬─────────────┘
                            ▼
                   ┌──────────────────┐
                   │  Core API        │  question selection, Leitner,
                   │  FastAPI         │  entitlement, stats, webhooks
                   └────────┬─────────┘
                            ▼
                        SQLite (WAL)
```

Both clients are thin. No business logic in the bot handlers, none in the
frontend. If the bot writes to SQLite directly while the Mini App goes through
an API, you will have two implementations of the Leitner rules within a month
and they will disagree.

### 6.2 What belongs on which surface

| Surface | Role |
|---|---|
| **Bot (chat)** | Mandatory. Onboarding, `/start`, quick drills, daily reminder, **payment**, notifications, entry point to the Mini App |
| **Mini App** | Optional but better. Sustained study sessions, translation toggle as a UI switch, exam simulation, stats dashboard, settings |

The exam simulation is the strongest argument for the Mini App: 30 timed
questions in chat is 30+ messages and an awkward timer. In a webapp it's one
screen.

**Do payments in the chat, not in the Mini App.** Mini Apps selling digital
goods sit closer to the Stars-only rule and to Apple's review guidelines. A
Tribute card link opened from a chat message is the cleaner path.

### 6.3 Security — the one that will bite

Telegram Mini Apps authenticate via `initData`, which **must be validated
server-side** with an HMAC using the bot token. Without it, anyone can craft a
request claiming any Telegram ID and unlock paid content for free.

Related: **never trust the client for entitlement.** Hiding the explanation in
the frontend is not a paywall. The API checks `pass_expires_at` on every
request that returns a translation or explanation, and returns nothing if the
pass has lapsed.

### 6.4 Infrastructure

- **Bot:** Python, aiogram 3, long polling (webhook later if it grows)
- **DB:** SQLite in WAL mode — genuinely fine for thousands of users. Design the
  schema so a Postgres migration is possible; don't do it pre-emptively.
- **Hosting:** single container on Coolify, mounted volume for the DB
- **Backups:** the DB holds user progress and subscription state — the only
  irreplaceable thing in the system. Automated, off-box, tested restore.
- **Image handling:** cache Telegram `file_id` after first send. With thousands
  of users, re-uploading sign figures would be your entire bandwidth.
- **Broadcasts:** Telegram allows roughly 1 msg/sec per chat, ~30/sec overall.
  Any daily reminder needs a throttled queue and handling for users who blocked
  the bot.
- **Content updates:** the Ministry reissues the listato periodically. Build
  seeding to diff against the DB and regenerate only changed questions.

---

## 7. Milestones

| # | Milestone | Exit criteria |
|---|---|---|
| M0 | Viability check | Net-per-subscriber confirmed; commercialista consulted; price shape decided |
| M1 | Question data | `questions.json` complete, 30 hand-verified, figures extracted |
| M2 | One topic end-to-end | One topic translated + explained + 100% reviewed. **Measure your correction rate** — this number tells you the cost of the whole bank. |
| M3 | Core API + bot | FastAPI owns logic; bot is a thin client. Core loop, translation toggle, Leitner, stats. Free, no paywall. |
| M4 | Closed beta | 5–10 real learners, chat only. Watch where they get confused. |
| M5 | Payments | Tribute digital products created, webhook handler live (signature verified, idempotent), refund revocation working, both tiers purchasable |
| M6 | Mini App | Study session, exam simulation, stats. `initData` validated server-side. |
| M7 | Public launch | 3–4 topics fully live and reviewed, both surfaces working |
| M8 | Full bank | Remaining topics, topic by topic |

M2 is the decision point. If the correction rate is high, the grounded-generation
approach needs rework before you commit to the full bank.

---

## 8. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| No distribution — nobody finds the bot | **Critical** | Telegram has no organic discovery. Treat §10 as a workstream with the same weight as content, starting at M1 not M6. |
| Wrong explanation reaches a paying user | High — reputational, they paid for accuracy | 100% human review, topic-gated release, report button |
| Parser misaligns answer key | Critical — silent, poisons everything | Hand-verify gate at M1; contradiction check at M3 |
| Stars economics make €3 unviable | High | Resolve at M0, before any code |
| Fiscal setup too heavy for the revenue | High | Commercialista at M0 |
| Structural churn — everyone leaves after passing | Medium | Price as a pass, not a subscription; referral loop |
| Saturated free competition | Medium | Compete on translation + explanation, not on quiz mechanics |
| Ministry reissues the listato | Low | Diff-based reseeding |
| Two frontends double the maintenance | Medium | Core API owns all logic; both clients stay thin |
| Mini App auth bypassed, paid content free | High | Validate `initData` server-side; entitlement checked on every API response |
| Solo-founder time | Medium | Topic-gated releases mean value ships continuously |

---

## 9. Success metrics

**Instrument from the first commit.** You cannot backfill events. Minimum event
log: question served, answer given (correct/wrong), translation toggled,
explanation viewed, paywall hit, paywall dismissed, purchase started, purchase
completed, session start/end. Everything above is derived from these.

- **M2:** explanation correction rate (target: <10% need editing)
- **M4:** beta users completing >50 questions
- **M6:** free → paid conversion (a realistic target for this category is low
  single digits; set the number after beta, not before)
- **Ongoing:** per-topic error rates — validates the content *and* is the
  product's core value
- **Ongoing:** report-button volume per 1,000 questions served

---

## 10. Go-to-market

**This is the biggest hole in the plan.** Telegram bots have effectively no
organic discovery — there is no App Store, no search that matters. A perfect bot
with no distribution earns nothing, and this section currently has less thought
in it than the Leitner algorithm does.

### 10.1 Competitive audit (do before M1)

Build an actual table, not an impression: name, price, languages, whether they
explain answers, whether they're a bot or an app, review count. You need to know
precisely what the free alternatives fail to do before you commit months to
beating them. If one of them already does Russian explanations well, the
positioning has to change.

### 10.2 Channels, roughly in order of likely return

| Channel | Notes |
|---|---|
| Expat Telegram communities | Russian, Ukrainian, English-speaking groups in Italy. This is where your exact user already is, and it's free. **Read each group's rules — most ban promotion, so lead with value, not links.** |
| Autoscuola partnerships | A school with foreign students has a problem you solve. Offer revenue share or free passes. Slow but high-trust. |
| Facebook groups | "Russians in Italy", "Expats in Rome" etc. Same logic as Telegram. |
| Reddit | r/italy, r/expats — low volume, but permanent and searchable |
| SEO / landing page | Long game. "quiz patente in russo" has low competition. Worth a static page from day one for the privacy policy anyway. |
| Short-form video | Demo of a hard question explained in Russian. Highest ceiling, most effort. |
| Referral | Tribute has a referral programme; a free week for a friend who converts is cheap. |

### 10.3 Beta recruitment

M4 needs 5–10 real learners. They come from 10.2 — recruiting them *is* the
first distribution test. If you can't find ten willing testers in the expat
groups, that's a signal about the channel, not just about the beta.

### 10.4 Launch sequence

Soft launch to one community first, fix what breaks, then broaden. A bad first
impression in a 20,000-member group is not recoverable.

---

## 11. Budget & effort

### 11.0 ⚠️ Do the Italy-dependent work before leaving

The product serves an Italian market and will be built from Uzbekistan. Most of
it travels fine — Telegram is remote-native, the expat communities are online,
the PDF doesn't care where you are. But a few things are much harder at 4,000km:

| Do before departure | Why |
|---|---|
| Recruit the autoscuola istruttore for expert review | Far easier to arrange in person; you can pay for a session and talk it through |
| Autoscuola partnership conversations | Walk in, explain, leave a card. Cold email from abroad converts far worse. |
| Beta tester recruitment | Local expat groups; ideally people you can actually meet once |
| Sample-of-20 validation test | The weekend Google Doc test — do it while your testers are nearby |
| Any Italian banking or documentation | Closing things is always easier from inside the country |

Also settle the exam format question (30 vs 40 questions) while you have easy
access to a real autoscuola — sources disagree and your simulator depends on it.

The plan currently has no numbers. It needs them before M0 closes.

**One-time content costs (estimate these properly at M2):**

- Explanation generation, Italian canonical — after deduplication
- Translation to RU + EN — questions and explanations
- Expert review by an istruttore — a few hundred euros
- Your review time — **the real cost.** Measure it at M2: minutes per
  explanation × cluster count is the number that decides whether this is a
  three-month or a twelve-month project.

**Recurring:**

- Hosting: one container plus the DB — small
- Domain
- Support time per user

**Use M2 to convert all of this from guesses into measured numbers.** A plan
with a wrong budget is still a plan; a plan with no budget is a wish.

---

## 12. Support & operations

- **Support channel:** a dedicated contact or `/support` command. Expect payment
  and access issues to dominate — "I paid and nothing happened" is the #1
  ticket for any webhook-based paywall.
- **Canned responses** for the top five issues, in RU/EN/IT.
- **Manual override:** an admin command to grant or extend a pass by hand. You
  will need this the first time a webhook is missed, and you'll need it at 11pm.
- **Monitoring:** you already run Beszel. Add alerts for the bot process, the
  API, and specifically **failed webhook deliveries** — a silent webhook failure
  means paying users locked out and no error anywhere.
- **Backups:** the DB holds progress and entitlement. Automated, off-box, and
  **test a restore before launch**, not after.
- **Bot UI localization:** interface strings need RU/EN/IT too, not just the
  questions. Put them in a translation file from the first commit — retrofitting
  hardcoded strings is miserable.

---

## 13. Kill criteria

Decide these now, while you're unattached to the outcome.

| Checkpoint | Stop or rethink if |
|---|---|
| M0 | Tribute/fiscal setup makes the price unviable |
| M2 | Explanation correction rate is so high that reviewing the full bank exceeds the time you're willing to spend |
| M4 | Beta users don't return for a second session unprompted |
| M6 + 3 months | Conversion is far enough below expectation that the remaining topics aren't worth generating |

Written down in advance, these are decisions. Discovered later, they're sunk
costs.

---

## 14. Implementation plan

### 14.1 Stack

| Layer | Choice |
|---|---|
| Language | Python 3.12 |
| Core API | FastAPI + Uvicorn |
| Bot client | aiogram 3 (long polling; webhook later) |
| DB | SQLite (WAL) via SQLAlchemy, Alembic for migrations |
| Mini App | Plain TS + Vite, or Svelte. Avoid a heavy framework — it's five screens. |
| Content pipeline | Standalone scripts, not part of the runtime service |
| Deploy | Docker → Coolify |
| Tests | pytest, focused on entitlement + Leitner + webhook idempotency |

### 14.2 Repo layout

```
patente-bot/
├── api/              # FastAPI — owns ALL business logic
│   ├── routes/       # questions, answers, stats, entitlement, webhooks
│   ├── services/     # selection, leitner, entitlement, events
│   ├── models/       # SQLAlchemy
│   └── migrations/   # Alembic
├── bot/              # aiogram — thin client, HTTP calls to api/
├── webapp/           # Mini App — thin client, HTTP calls to api/
├── content/          # one-off pipeline, run manually
│   ├── extract.py        # PDF  → questions_raw.json
│   ├── normalize.py      # raw  → clean, validated
│   ├── cluster.py        # dedupe into rule clusters
│   ├── generate.py       # clusters → IT explanations (LLM + CdS articles)
│   ├── translate.py      # approved IT → RU/EN
│   ├── review_export.py  # → CSV for review
│   ├── review_import.py  # CSV → DB, marks approved
│   └── seed.py           # load into DB
├── shared/           # config, constants
└── tests/
```

The rule that keeps this from becoming two codebases: **`bot/` and `webapp/`
contain no business logic and no DB access.** Both call the API.

### 14.3 Schema

```sql
questions      id, topic, stem_it, statement_it, answer, image_path,
               cluster_id, source_version
translations   question_id, lang, stem, statement
clusters       id, rule_summary          -- dedup grouping
explanations   cluster_id, lang, text, status, reviewed_at, reviewer
users          chat_id, lang, translations_on, pass_expires_at,
               free_explanations_used, created_at
progress       chat_id, question_id, box, due_at, seen, wrong
purchases      id, chat_id, tribute_purchase_id, tier, amount,
               created_at, refunded_at
events         id, chat_id, type, payload, created_at
```

**Explanations key off `cluster_id`, not `question_id`.** That is the whole
payoff of deduplication: one approved explanation serves every reworded variant
of the same rule, and a correction lands in one row.

`source_version` lets you diff against a reissued ministerial listato and
regenerate only what changed.

### 14.4 Build order

Dependency-ordered. Each step is testable before the next starts.

| # | Step | Depends on | Notes |
|---|---|---|---|
| 0 | Repo, Docker, Alembic skeleton | — | half a day |
| 1 | **`extract.py` → questions.json** | — | **critical path.** Nothing else can be verified without real data. |
| 2 | Schema + `seed.py` + hand-verification of 30 questions | 1 | the quality gate |
| 3 | Core API: selection, Leitner, answer recording, stats | 2 | pure logic, easy to test |
| 4 | Event logging | 3 | **do it here, not later** — you cannot backfill |
| 5 | Bot client: `/start`, `/quiz`, answer, `/stats` | 3 | first thing you can actually use |
| 6 | `cluster.py` + `generate.py` for ONE topic | 2 | this is M2 — measure correction rate |
| 7 | Review loop (CSV out, CSV in) | 6 | see 14.5 |
| 8 | `translate.py` for approved IT → RU/EN | 7 | |
| 9 | Entitlement checks + Tribute webhook + purchases | 3 | |
| 10 | Mini App | 3, 9 | |
| 11 | Exam simulation | 10 | |

Steps 1–5 give you a working free bot you can study with yourself. That is the
first real checkpoint and it doesn't depend on any blocked M0 item — **build it
while waiting for Tribute and the accountant to reply.**

### 14.5 Review tooling — don't build it

You have hundreds of clusters to read. The instinct is to build a review UI.
Don't. Export to CSV, review in Google Sheets (filter by "contains a number",
sort by topic, mark approved in a column), import back. Zero build time, works
offline, and you can hand a sheet to the istruttore or a native-speaker reviewer
without giving them access to anything.

Build a review UI only if the spreadsheet actually becomes the bottleneck.

### 14.6 Environments & secrets

- Local: SQLite file, `.env`, polling bot against a separate test bot token
- Prod: Coolify, env vars, volume-mounted DB, automated off-box backup
- **Two bot tokens** — a dev bot and a prod bot. Testing against the live bot
  while users are on it is a bad afternoon.
- Never commit tokens, the Tribute API key, or the webhook secret.

### 14.7 Definition of done for step 1

`extract.py` produces `questions.json` where:

- every record has topic, statement, answer
- clustered questions retain their stem
- figure-based questions have a valid image path
- total count matches the PDF's stated count
- 30 randomly sampled records match the PDF by hand

---

## 15. Open decisions

**Settled:**

1. ✅ Languages — Russian + English at launch, schema built for more
2. ✅ Price — €2.99 / 1 month, €6.99 / 3 months, both one-time (no auto-renew)
3. ✅ Free tier — plain question + Vero/Falso + correct answer only
4. ✅ Payment rail — Tribute, card link preferred over Stars
5. ✅ Surfaces — both Mini App and in-chat, sharing one core API

**Still open:**

5. ❓ Free explanation allowance (recommend 3–5 lifetime, as a quality taster)
6. ❓ Scope: full bank at launch, or highest-failure topics first
7. ❗ Tribute: merchant of record and EU VAT — get it in writing *(blocking)*
8. ❗ Commercialista consultation *(blocking)*

Items 7 and 8 remain M0 blockers. Everything else can be decided during M1–M2.
