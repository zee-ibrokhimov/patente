# Status & handover

**Last updated:** 29 July 2026 (late — corpus ingested, clusters written)
**Bot:** [@quizpatente_bot](https://t.me/quizpatente_bot) — working, free tier only
**Tests:** 152 passing
**Plan:** [patente-bot-plan.md](patente-bot-plan.md) · **How to run:** [README.md](README.md)

> **Read first:** §2 is a measurement that changes the plan, and §4 is the short
> list of things only you can do.

---

## 1. Where things stand

| Build step (§14.4) | Status |
|---|---|
| 0 · Repo, config, Alembic skeleton | ✅ done (Docker not written — Docker isn't installed here) |
| 1 · `extract.py` → questions.json | ✅ done, validated |
| 2 · Schema + `seed.py` + verification gate | ✅ done — **hand-verification still owed by you**, see §4 |
| 3 · Core API: selection, Leitner, stats | ✅ done |
| 4 · Event logging | ✅ done |
| 5 · Bot client | ✅ done — live and usable |
| 6 · `cluster.py` | ✅ done — **3382 clusters written to the database** |
| 6 · `fetch_norms.py` (CdS + Regolamento) | ✅ done — **corpus fetched**, 647 articles / 1.5 M chars |
| 6 · Topic → article map | 🟡 measured, see §9 — less mechanical than hoped |
| 6 · `articles.py` (topic → article) | 🟠 written, **never executed** — see §11 |
| 6 · `generate.py` | 🟠 written, **never executed** — see §11 |
| 7 · Review loop (CSV out/in) | 🟠 written, **never executed** — see §11 |
| 8 · `translate.py` | ⬜ not started (unblocked) |
| 9 · Entitlement + Tribute webhook | ⛔ blocked on Tribute credentials |
| 10 · Mini App | ⬜ not started |
| 11 · Exam simulation | ⬜ not started |

Commits: `a381bbb` extract → `b7febf1` schema/seed → `f58cf7a` API → `1283152` bot → cluster.

**The environment does not survive being moved.** This repo now lives under
OneDrive and was opened on a second machine, where nothing ran: `.venv/pyvenv.cfg`
still pointed at `C:\Users\Workstation\…\Python312`, which does not exist here.
A venv is never portable — delete it and rebuild (§6). Two latent packaging bugs
surfaced while doing so and are fixed: `pyproject.toml` had no package list, so
setuptools refused a flat layout with five top-level directories; and `numpy` was
never declared even though `rapidfuzz.process.cdist` — the similarity matrix
`cluster.py` is built on — imports it lazily. Both would have hit the first
deploy just as hard as they hit this laptop. Now on Python 3.14.

**The content bank is real and verified:** 7106 statements, 715 quesiti, 25 topics,
409 figures. All 590 PDF pages contributed; an independent recount from raw word
coordinates found zero statements missing and zero invented, and the answer census
(3542 VERO / 3564 FALSO) matches exactly.

---

## 2. ⚠️ The finding that changes the plan

`cluster.py` was run to measure what §11 calls the real cost — *minutes per
explanation × cluster count*. **The bank is two completely different populations,
and the plan's premise that "a large share of the bank is the same rule reworded"
is only half true.**

| | statements | clusters | reduction |
|---|---|---|---|
| **Sign / figure questions** | 3946 | **426** | **9.3×** |
| **Text-only questions** | 3160 | **2951** | **1.1×** |

Text-only statements barely deduplicate, and that is the data rather than the
tuning. Only 4% have a near-twin (similarity ≥95); 73% have nothing closer than
87. Lowering the threshold does not help — at 70 you reach 3.0× but cluster
cohesion collapses from 90 to 63 and the largest cluster balloons to 553
statements, which is chaining producing garbage.

### What it costs

**3382 clusters ⇒ ~169 hours of review at 3 min each, ~282 h at 5 min.**
That is 56–94 evenings for the full bank — not the few weekends the plan implies.
This is the number §13's M2 kill criterion needs, and it was previously a guess.

### The strategic consequence — effort is inverted from intuition

The *largest* topics are the *cheapest*, because sign questions collapse 9–16×:

| clusters | statements | hrs @3min | topic |
|---:|---:|---:|---|
| 15 | 243 | 0.8 | Segnali di precedenza |
| 19 | 179 | 0.9 | Segnali complementari |
| 30 | 426 | 1.5 | Segnali di obbligo |
| 41 | 252 | 2.0 | Segnalazioni semaforiche |
| 43 | 414 | 2.1 | Esempi di precedenza |
| 46 | 502 | 2.3 | Segnali di divieto |
| 48 | 259 | 2.4 | Segnaletica orizzontale |
| 54 | 662 | 2.7 | Segnali di pericolo |
| 63 | 603 | 3.1 | Segnali di indicazione |
| 71 | 260 | 3.5 | Pannelli integrativi |
| … | | | *(15 text topics: 147 h for the remaining 46% of the bank)* |
| 492 | 531 | 24.6 | Definizioni stradali e di traffico |

**Ship the sign topics first.**

- first 3 topics → 3.2 h of review → **11.9%** of the bank live
- first 5 topics → 7.4 h → **21.3%**
- **first 10 topics → 21.5 h (7 evenings) → 53.5% of the bank fully explained**

### Correction to an earlier recommendation

I previously suggested **"Distanza di sicurezza"** for M2 on the reasoning that it
is small, text-only and maps to one article. **The data says that is wrong.** It is
109 clusters / 5.5 h of review for only 134 statements (1.9% of the bank).
**"Segnali di precedenza"** is 15 clusters / 0.8 h for 243 statements (3.4%) —
roughly 7× the value per hour of your time. Do a sign topic for M2.

---

## 3. Decisions I made — reverse any you disagree with

| Decision | Where | Why |
|---|---|---|
| Extraction is geometric, not text-flow | `content/extract.py` | On 68 rows the PDF glues VERO/FALSO onto the statement line; reading-order parsing shifts the whole answer key by one from there on. Three other defects documented in the module docstring. |
| Figures belong to the **statement**, not the quesito | schema | 139 comparison items ("il segnale (A) … (B)") carry their own composite image differing from their group's. |
| Clustering never crosses a topic | `content/cluster.py` | Explanations key off `cluster_id` and §3.3 releases per topic; a cross-topic cluster would block both topics from shipping independently. |
| Three content states, not two | `api/services/entitlement.py` | `locked` (pay for it) vs `unavailable` (nobody wrote it). Showing a paywall for content that doesn't exist sells something undeliverable. Only **approved** explanations are ever served — a draft reads unavailable even to a paying user. |
| Spaced repetition + stats are **free** | `shared/constants.py` → `REQUIRE_PASS_FOR_*` | §4.3 lists them as *recommended* paid, not decided. Both cost nothing to serve and a returning free user hits the explanation paywall more often. One-line flip. |
| Wrong answer drops to box 1 from any height | `api/services/leitner.py` | Demoting one step lets a repeatedly-missed question hover near the top and return a week later. |
| Box 1 = 10 minutes, not 0 | `shared/constants.py` | Back within the session, but with ~10 questions in between; at 0 it is served immediately next and trains recall of the last screen. |
| GDPR erase **anonymises** events, deletes everything else | `api/services/users.py` | Deleting event rows would silently rewrite historical conversion numbers. Purchases retained for accounting and refund matching. |
| Seeding is diff-based, topics keyed by **name** | `content/seed.py` | `extract.py` numbers topics alphabetically; one new ministerial topic would renumber the rest and re-point every question. |
| Clusters have a **natural key**, not a positional id | `api/models/content.py`, migration `9c1d4a7b2e50` | See §10. Re-running `cluster.py --write` used to delete every explanation in the database, silently. |
| The ministerial answer is **withheld** from the generator and used to test it | `content/generate.py` | Handing the model the statement, the answer *and* the article produces a prompt that cannot fail: it will justify whatever key it is shown, including a key that `extract.py` got wrong. The model decides VERO/FALSO from the article text alone; the stored answer is compared afterwards. That is plan §3.3's second gate implemented as a real test rather than a formality. |
| Over-coarse clusters are split by a **hand list**, not an algorithm | `content/cluster.py` → `SPLIT_BY_QUESITO` | Six statistical criteria were measured and none separates "four rules in one cluster" from "one rule asked thirty ways". The docstring records all six so nobody spends a day rediscovering them. One entry today: the semaforo. |

---

## 4. ⛔ Blocked on you

1. **Rotate both API keys before launch.** The bot token `8918020834:AAE…` was
   pasted into a chat transcript, and the OpenAI key was read from `.env` during a
   session. @BotFather → `/revoke`; OpenAI dashboard → revoke and reissue.
   Not urgent for local work, mandatory before anything public.

   The first OpenAI key has since been revoked — it turned up as a 401 on the first
   real `generate.py` run — and its **replacement was also pasted into a chat
   transcript**, so it is burnt for any use beyond local content generation. Treat the
   key in `.env` as disposable: finish the offline runs with it, then reissue. The
   pattern to break is putting the key anywhere other than `.env`; nothing in this
   repo ever needs it typed, and `shared/config.py` is the only thing that reads it.
2. **Hand-verification** — open `content/out/verify.html` (regenerate with
   `content/verify_sample.py`) beside the PDF and tick off the 30 random + 24
   high-risk rows. **Do this before generating explanations**, or a parsing error
   gets baked into content you then pay a human to review.
3. **Read the 48 flagged clusters once.** `cluster.py --report` now prints how many
   clusters are worth a closer look (≥20 statements, or drawn from ≥3 ministerial
   quesiti): 48 clusters, 973 statements. Open `content/out/clusters.html`, and if
   any of them holds more than one rule, add its `(topic_id, image_path)` to
   `SPLIT_BY_QUESITO` and re-run. Do this **before** `generate.py`, because after
   that a split costs you the explanations for the cluster it splits.
4. **Decide the generation model.** `.env` says `gpt-4o`; your key also has
   `gpt-5`, `gpt-5-mini` and `gpt-5-pro`. Explanation generation is a *one-time,
   offline* cost that does not scale with users (plan §4.4), and accuracy of legal
   reasoning is the entire product. Using a two-generation-old model to save a few
   euros on a one-off run is the wrong trade — **recommend `gpt-5` for Italian
   explanation generation**, `gpt-5-mini` for the bulk translation pass.
5. **Tribute**: creator verification, both products created, API key + webhook
   secret. And ❗ **the merchant-of-record / EU VAT question in writing** (plan §4.1).
6. **Commercialista consultation** (plan §5). Still an M0 blocker for payments.

---

## 5. Next steps, in order

1. **Verify the bank** (§4.2 above) — 30 minutes, gates everything downstream.
   Still owed, and still the one thing nobody else can do.
2. ~~Cluster strategy~~ ✅ **done** — `--strategy figure`, 3382 clusters, written.
   Re-running is now safe and idempotent (§10). Read the 48 flagged clusters
   (§4.3) before generating anything.
3. ~~Codice della Strada ingestion~~ ✅ **done** — 647 articles in
   `content/out/norms/`. Three parser defects found and fixed on the way; see §8.
4. **Topic → article map.** Measured, and it is *less* mechanical than §8 hoped —
   see §9 for the numbers and what actually works.
5. **`generate.py` for ONE sign topic** — this is M2. Measure the correction rate.
6. **Review loop**: CSV out → Google Sheets → CSV in. Do *not* build a review UI (§14.5).
7. Then `translate.py`, then payments.

**Not code, but the plan says start now:** the competitive audit (§10.1) and the
Italy-dependent work in §11.0 — recruit the istruttore, talk to autoscuole, find
beta testers — all easier before leaving Italy.

---

## 6. Running it

Two processes. Both were left running on the work machine; stop with
`taskkill /F /IM python.exe`.

```bash
.venv/Scripts/python.exe -m uvicorn api.main:app --port 8000
.venv/Scripts/python.exe -m bot.main
```

`GET /health` reports whether content is loaded. Full command list in the README.

To rebuild from scratch on another machine:

```bash
py -3.12 -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[content,dev]"
.venv/Scripts/alembic.exe upgrade head
.venv/Scripts/python.exe content/seed.py
```

The listato PDF is **not committed** (24 MB, gitignored) — it lives in
`questions/`. `content/out/questions.json` and the 409 figures *are* committed, so
seeding works without it.

---

## 7. Known gaps

- No Dockerfile yet — Docker isn't installed on this machine.
- No backups configured. The database holds progress and entitlement, the only
  irreplaceable data in the system (§6.4). Needed before any real users.
- No admin command to grant or extend a pass by hand (§12). You will want this the
  first time a webhook is missed.
- `bot/` has no automated end-to-end test against Telegram — handlers are covered
  only through the render and i18n layers.
- The 15 text-only topics are 87% of the review effort for 46% of the bank. Worth
  deciding whether the full bank is the goal, or whether the sign topics plus the
  highest-failure text topics is a better product (plan §15, open decision 6).

---

## 8. The legal corpus (`content/out/norms/`)

`content/fetch_norms.py` pulls both statutes from **Normattiva**, the authoritative
consolidated text. Its URN permalinks resolve without JavaScript, which every other
route into that site does not:

```
https://www.normattiva.it/uri-res/N2Ls?urn:nir:stato:decreto.legislativo:1992-04-30;285~art142
```

Italian statutes are *atti ufficiali dello Stato* and fall outside copyright
(art. 5, L. 633/1941), so the text is free to reuse — attribute it anyway. This is
also what makes the explanations defensibly yours: they are derived from statute,
not from an autoscuola manual (plan §3.3).

```bash
.venv/Scripts/python.exe content/fetch_norms.py --source both
.venv/Scripts/python.exe content/fetch_norms.py --source reg --first 77 --last 136   # signs only
```

Re-running is incremental — articles already saved are skipped, so an interrupted
run resumes. Output is `cds.json` and `regolamento.json`, each article carrying its
number, rubric, full text, referenced figure plates, and the sign names it defines.

**What is actually in there now:** CdS 239 articles / 769 k characters (all but
art. 1 and the five repealed tail articles), Regolamento 408 / 738 k, 244 distinct
figure plates. `fetch.log` is gitignored; the two JSON files are committed, so
nobody has to re-crawl Normattiva.

### Three defects the first run hid

Running it for real was worth more than reading it. All three are fixed, and
`tests/test_fetch_norms.py` pins each one.

1. **Silent truncation, and it was most of the corpus.** The body was captured
   with a non-greedy regex up to the first `</div></div>`, but Normattiva wraps
   every passage amended since 1992 in its own div — so the capture ended inside
   comma 1 of almost every article. The text that came back was genuine, just
   incomplete, which is the worst possible shape for grounding text. **Art. 148
   lost commi 3–16, i.e. every actual rule about overtaking**, while topic 12
   *Norme sul sorpasso* has 156 statements resting on it. Art. 142 lost the
   speed-camera and sanction commi; art. 187 came back empty. Counting div depth
   instead recovered 769 k characters where the old parser found 479 k.
2. **A repealed article overwrote a real one.** Normattiva does not 404 an article
   that does not resolve — it serves the decree's *preamble*, whose first `Art. N`
   belongs to some other law cited in the recitals. Every unresolvable article
   therefore parsed as "art. 4" and the last one won, replacing the real art. 4
   with legislative boilerplate. The fetcher now refuses any page whose number is
   not the one it asked for, and prints the numbers that came back empty rather
   than only counting them.
3. **Amendment history was being kept as if it were law**, and articles rewritten
   since 1992 lost their rubric because the parser required a bare `1.` where the
   consolidated text writes `((1.`.

Between them these three are the difference between explanations grounded in the
Codice and explanations grounded in the first sentence of the Codice.

**The Regolamento is the important one for the topics you should ship first.** Signs
are defined there rather than in the Codice, and every sign is named in capitals and
cross-referenced to a plate:

```
Art. 116 (Segnali di divieto generici)
  a) il segnale DIVIETO DI TRANSITO (fig. II.46);
  b) il segnale SENSO VIETATO (fig. II.47);
```

Those capitalised names are extracted into `sign_names` per article, which is what
lets a sign cluster be matched to the article that governs it. §9 is the measurement
of how far that actually gets, because it is not as far as this paragraph assumed.

---

## 9. Topic → article mapping: what the data allows

The claim in §8 was that sign topics could be matched to articles mechanically,
because the Regolamento names every sign in capitals. **Measured, it is much weaker
than that, and for a reason worth writing down.**

`sign_names` as first extracted is any run of capitals ≥6 characters, which is too
loose — it collects STRADA, MOTORE, TONNELLATE alongside real sign names, and the
noise dominates. Restricting it to a capitalised name immediately bound to a plate
(`il segnale DIVIETO DI TRANSITO (fig. II.46)`) gives **144 clean sign names**
instead of 251 noisy ones.

That fixes precision. It does not fix the real problem:

> **The ministerial statements identify a sign by its picture, not by its name.**
> "Il segnale raffigurato indica un divieto di transito" never contains the words
> the Regolamento indexes on. Matched statement by statement, coverage is 25–40%
> even on the sign topics, and under 5% on the text topics.

Voting across a figure looked like the answer — every statement sharing a figure is
about the same sign, so one statement that names it should name the whole cluster.
That produces a match on **261 of 409 figures**, and I first reported that as "64%
identified". **That number is wrong and the error matters**: it counts matches, not
correct identifications. Tested against three clusters, the match named the wrong
sign on two of them, because the statements mention neighbouring signs constantly —
cluster 624 is a stop sign whose statements refer to the DIRITTO DI PRECEDENZA of the
road it faces. Substring matching cannot separate "is" from "faces". See §12.

So the matches are used only to *order* the article context, where being wrong costs
some irrelevant statute rather than a wrong answer.

**So the map is: mechanical for ~two thirds of sign clusters, hand-written for the
rest and for all 15 text topics.** That is still most of an afternoon saved, but
plan §3.3's "hand-map the 25 topics → relevant articles" remains a real task rather
than a derived artefact. Do it at topic granularity (25 rows, cheap, and it gives
`generate.py` a fallback for every cluster the figure vote misses), then let the
sign-name vote override it per cluster where it fires.

---

## 10. The bug that would have eaten the content bank

Worth reading even though it is fixed, because it explains a schema column and a
migration, and because it is the failure mode this project can least afford.

`cluster.py --write` used to assign cluster ids positionally —
`enumerate(sorted(...), 1)` over freshly built clusters — after deleting every
existing row. Meanwhile `explanations.cluster_id` is `ON DELETE CASCADE` and
`shared/db.py` sets `PRAGMA foreign_keys=ON` (it must, or GDPR erasure silently
does nothing).

Those three facts together mean: **re-running the clustering step after generating
explanations deleted every explanation in the database, and reported success.**
Verified on a throwaway database, not inferred. Adding one entry to
`SPLIT_BY_QUESITO` was enough to move 2692 of 3377 ids.

This was not a hypothetical. The whole content pipeline is built around the
Ministry reissuing the listato: `seed.py` detaches changed questions from their
cluster precisely so they can be re-clustered. The first reissue would have
destroyed months of paid review.

The fix is `clusters.natural_key` — a cluster identified by what it is *about*
(`t17|fig:images/d603ba63c2155410.jpeg`, or `t4|txt:1893` for a text cluster keyed
by its lowest statement number) rather than by where it landed in a sort. `persist()`
now matches on that key and reuses the existing id, and a rerun reports
`0 new, 3382 kept, 0 removed`. A rule that genuinely leaves the listato still takes
its explanations with it — that is correct — but `--write` refuses to do it without
`--force` once explanations exist.

Migration `9c1d4a7b2e50`. It was nearly free to add while `clusters` was empty and
would have been expensive later, which is the only reason it was done now.

---

## 11. Written blind — now under test, not yet exercised end to end

Command execution became unavailable partway through the session and did not come
back, so the files below were written without being run once. **Both migrations have
since been applied and the suite passes at 195**, so they import and their unit
behaviour holds. What has *not* happened is a single run against the real corpus and
the real database.

| File | What it is |
|---|---|
| `content/articles.py` | the 25-topic → article map, plus the sign-name index |
| `content/generate.py` | M2 — cluster + statute → Italian explanation |
| `content/review_export.py` | step 7, out |
| `content/review_import.py` | step 7, in — the only thing that sets `approved` |
| `tests/test_articles.py`, `tests/test_generate.py`, `tests/test_review.py` | 43 tests for the above, all passing |
| `explanations.flags` + migration `b4e2f83c17a9` | why a draft was flagged — applied |
| `fetch_norms.py --reparse`, tighter `SIGN_RE` | 144 real sign names instead of 251 noisy ones |

Also checked statically, because the tests cannot: all 25 `TOPIC_ARTICLES` keys are a
prefix of exactly one ministerial topic name, no key is a prefix of another, and every
article number referenced falls inside the fetched range. `grounding()` skips articles
missing from the corpus, so a wrong number degrades to less context rather than a crash.

**The saved corpus still holds the loose sign names** — `--reparse` has not run.
`articles.py` re-derives them itself when the `signs` field is absent, so nothing
breaks either way, but the JSON on disk is a version behind the parser.

Since verified: `--reparse` ran (CdS 207 sign names → **0**, which is the correct
answer — the Codice defines no signs, so all 207 were noise; Regolamento 373 → 156),
and `generate.py --dry-run` exercised the whole path except the API call. It reported
**0 clusters with no article**, and the prompt led with the articles the cluster's own
statements named rather than the topic's numerical order — so the §9 sign-vote is
working, not just the hand-mapped floor.

The review loop has since been exercised end to end against a throwaway database
(`DATABASE_URL=sqlite:///…` overrides the real one): draft → export → decision →
import → release report, plus the refusal path. Three defects came out of doing that
rather than reasoning about it:

  · **`release_report` crashed on the ✅ it printed** when a topic reached 100%. The
    Windows console is cp1252 and U+2705 has no mapping there, so the single most
    important message in the review loop was the one message guaranteed to fail — and
    it failed *after* the commit, so the import had actually worked. Now ASCII, and
    all three content scripts set `sys.stdout.reconfigure(errors="replace")` so an
    unmappable character costs a glyph rather than a run.
  · **`generate.py` committed once, at the end.** An exception at cluster 300 of 3382
    would have rolled back 300 clusters already paid for. It now commits per cluster,
    which is strictly better given a rerun skips whatever is already written.
  · **A 401 was retried once per cluster.** Observed for real on the first run: fifteen
    identical authentication failures. `is_fatal()` now stops on anything that will
    fail the same way every time — bad key, exhausted quota, unreachable model — and
    exits non-zero, while rate limits and timeouts stay skippable.

Both scripts also stopped printing "committed" over transactions in which nothing was
written, which is how a reviewer comes away believing a sheet landed when every row
was refused.

The only thing never executed is the API call itself.

### Generation cost is not a constraint

Measured off the dry run: ~26 k characters of prompt, so roughly 7-8 k tokens per
cluster. That is well under a euro for a 15-cluster topic and on the order of €30-40
for all 3382. **This kills the model question in §4.4** — there is no money to save by
using an older model, and on current list prices gpt-5 costs less per token than the
gpt-4o `.env` still names, as well as reasoning better about law. Use gpt-5.

The budget that matters remains the 169 hours of human review, exactly as §11 of the
plan assumed.

### Next command, and it spends money

```bash
.venv/Scripts/python.exe content/generate.py --topic "Segnali di precedenza" --model gpt-5
```

**"Segnali di precedenza" is the right first topic**: 15 clusters, 243 statements,
0.8 h of review for 3.4% of the bank (§2). What comes out of it is the M2 number the
schedule has only ever guessed at — the fraction of drafts needing an edit or a
rejection.

---

## 12. First real generation: three clusters, and what they cost to learn

Run on clusters 623-625 of *Segnali di precedenza*, several times, for about €0.25
all in. Three drafts now exist, all naming the correct sign. Every finding below came
from running it, and none of them from reading the code.

**Cost is settled and it is not a constraint.** ~7 k tokens in and ~350 out per
cluster: **€0.02 a cluster, so €70-100 for all 3382** at gpt-4o list. My earlier
"€30-40" was extrapolated from gpt-5's cheaper input rate and was optimistic; either
way it is a rounding error against 169 hours of review.

**gpt-4o beat gpt-5-mini here.** 2 of 3 clusters answered versus 1 of 3, and
gpt-5-mini spent 9273 output tokens against gpt-4o's 1127. A small sample, but it is
the opposite of what the price list suggests, so do not assume the newer-and-cheaper
model is the better one for this job without checking.

**More context made it worse.** At `--context-chars 60000` all eleven precedenza
articles fit and **all three clusters declined** as "articles insufficient", against 2
of 3 at the 24 k default. Burying art. 106 among its neighbours made the model less
able to decide, not more. Keep the default; the "raise it to 40000" advice I gave
before measuring was wrong.

**"Articles insufficient" is partly noise.** The same prompt, unchanged, declined on
one run and answered 11/12 on the next. Roughly a third of clusters decline per run
and a re-run picks them up, which the idempotent write already handles for free.
Budget for two or three passes over a topic rather than one.

**The numeric gate was firing on every single draft.** The prompt requires a citation,
citations contain digits, `\d` matched "107". A gate that flags 100% of rows is a gate
the reviewer learns to ignore. Citations are now stripped before the check, and the
one true positive — art. 106's 25 m / 10 m placement distances — still flags.

**Naming the sign for the model: tried, measured, reverted.** See §9. It named the
wrong sign on 2 of 3 clusters and the model followed it into confidently wrong
explanations, having got both right unaided. **The figure is the ground truth and all
409 images are on disk, so the real fix is a multimodal call** — that is the single
highest-value open improvement, because sign topics are 54% of the bank.

### Decided 29 July: generate on demand, not up front

**New direction, and it reverses plan §3.3's "generate once, offline, never at
runtime".** The flow becomes: user answers a question → asks for the explanation →
the bot fetches it, serves it, and stores it so the next user pays nothing.

**Why it is the right call.** The 169 hours of human review were about to become the
launch gate for a product whose code is otherwise finished. Lazy generation removes
that gate entirely, and gets two things free that the offline plan could not:

  · **You only pay for clusters someone actually reaches.** Many of the 3382 will
    never be requested. The €70-100 is a ceiling, not a bill — and because results
    are cached per cluster it stays a ceiling no matter how many users arrive.
  · **Review priority stops being guesswork.** §2 agonised over which topic to review
    first. Demand answers it: review the clusters users actually hit, most-requested
    first. That is a far better ordering than topic size, and it only exists in this
    architecture.

**What it costs, and this is the part to decide deliberately.** §3.3's release rule
was "a topic goes live only when 100% of its explanations have been read by a human,"
and the reasoning was that an absent explanation is acceptable while a confidently
wrong one about a speed limit is not. On demand, **the first user to ask gets an
explanation no human has read** — and the explanation is the paid feature. That is the
one thing to get right; everything else here is mechanics.

The principled line, and my recommendation: **serve `draft`, withhold `flagged`.**
Anything that tripped a gate — argues against the ministerial answer, contains a
number, low model confidence — never reaches a user unreviewed, and reads as
`Access.UNAVAILABLE`, which `entitlement.py` already distinguishes from "pay for it"
(§3 decisions). Anything that passed every gate is served, and a human upgrades it to
`approved` later through the same step-7 loop.

That makes **the flag rate the number that matters now**, where it used to be the
correction rate. On the 3-cluster sample it was 1 in 3, which as a share of
explanation requests answering "not available" would be poor for a paid feature.
Worth measuring over a full topic before launch.

**Decided:** serve `draft`, withhold `flagged`.

**Translations move to demand too, and both languages come back in one call.** When a
user is served a question, RU and EN are requested together and cached in the existing
`translations` table — `(question_id, lang)` is already unique, so nothing changes in
the schema. §3.3's "translate the *approved* Italian explanation" pipeline is dropped:
`translate.py` is not being written.

**The explanation call must see the question and its figure.** Not the cluster in the
abstract — the actual statement, and the image where there is one. This is no longer an
optional improvement (§12's task list had it as "highest-value open"): it is required,
because a text-only model cannot resolve "il segnale raffigurato" at all, and the
explanation is the thing being sold. All 409 figures are on disk and gpt-4o accepts
them, so the change is contained to the call itself.

Explanations stay keyed on `cluster_id` and that stays correct: under the figure
strategy every member of a figure cluster shares one `image_path` — the figure *is* the
cluster key — so "the image for this question" and "the image for this cluster" are the
same picture. Text clusters have no image and lose nothing.

**The hot path is the problem to solve first.** Explanations are user-initiated, so a
few seconds of "sto preparando la spiegazione…" is fine. Translations are not: they
belong to *serving a question*, which is every interaction. Nobody waits 3-5 seconds
per question. Options, in the order I would try them:

  1. Serve the Italian immediately and edit the message when the translation lands
     (`editMessageText`); the user starts reading the question either way.
  2. Pre-warm — translate the next few questions in the background while the user is
     answering the current one. Selection already knows what is coming next.
  3. Both. (1) covers a cold cache, (2) means it is almost never cold.

Cost is small: ~7106 short calls if every question is eventually served, and
translations are a paid feature (§4.3), so only entitled users trigger any of it.

Mechanics that follow:

  · **The generation core has to move.** `content/` is one-off scripts and `api/` owns
    all business logic (README, and the rule that keeps bot and webapp thin). Prompt
    building, grounding and the gates belong in `api/services/`, with
    `content/generate.py` becoming a batch caller of the same code — useful for
    pre-warming a topic, no longer the only path.
  · **Latency is 5-15s.** The bot needs a "sto preparando la spiegazione…" message,
    and the API needs a per-cluster lock so ten users asking at once cause one call.
  · **OpenAI becomes a runtime dependency.** A 401, a quota, or an outage is now
    user-facing rather than a failed batch job. `is_fatal()` already classifies these;
    the serving path must degrade to `unavailable` and log loudly, never to a stack
    trace or a wrong answer.
  · **Declines are ~1 in 3 and noisy** (§12). On a cache miss that decline is a user
    staring at nothing, so retry once inline before giving up.
  · Entitlement already gates the spend: 3 free explanations, then a pass.

### What the flag actually looks like

The one flagged cluster is worth reading, because it is the design working:

> q20470 — "Il segnale raffigurato si trova sulle corsie di accelerazione per
> l'immissione in autostrada" — stored **VERO**, model said **FALSO**.

Neither is a mistake. Art. 106 Reg. does not mention motorway acceleration lanes, so a
model told to reason *only* from the article correctly answers FALSO; the ministerial
answer is correct about the real world. This is the third category the docstring
predicts — the statement is not governed by the article the topic maps to — and it
clears in seconds once a human looks. Expect a meaningful share of flags to be this,
not errors.

**Two failure classes, and only one of them is a vision problem.** Worth separating
before spending effort on §13's image work:

  · **"Which sign is this?"** The statements say only "il segnale raffigurato" and the
    model cannot see the figure. This is cluster 625's decline and the reason the
    name-guessing hack was tempting. **Sending the image fixes it outright.**
  · **"Is this fact about the sign true?"**, where the fact is not in the mapped
    article. q20470 is this: the model identified the sign correctly and cited art. 106
    accurately — the article simply does not mention motorway slip roads, and neither
    does a picture of a triangle. **Vision does not fix this.** The lever is the article
    map — sign topics probably want CdS 175-176 and 22 for autostrada placement — or
    letting a human clear it.
