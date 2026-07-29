# Status & handover

**Last updated:** 29 July 2026
**Bot:** [@quizpatente_bot](https://t.me/quizpatente_bot) — working, free tier only
**Tests:** 139 passing
**Plan:** [patente-bot-plan.md](patente-bot-plan.md) · **How to run:** [README.md](README.md)

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
| 6 · `cluster.py` | ✅ done · `generate.py` ⛔ blocked on OpenAI key |
| 7 · Review loop (CSV out/in) | ⬜ not started |
| 8 · `translate.py` | ⛔ blocked on OpenAI key |
| 9 · Entitlement + Tribute webhook | ⛔ blocked on Tribute credentials |
| 10 · Mini App | ⬜ not started |
| 11 · Exam simulation | ⬜ not started |

Commits: `a381bbb` extract → `b7febf1` schema/seed → `f58cf7a` API → `1283152` bot → cluster.

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

**3377 clusters ⇒ ~169 hours of review at 3 min each, ~281 h at 5 min.**
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

---

## 4. ⛔ Blocked on you

1. **`OPENAI_API_KEY` in `.env`** — blocks `generate.py` and `translate.py` (steps 6–8).
   Put it in the file directly; don't paste it into a chat.
2. **Rotate the bot token.** `8918020834:AAE…` was pasted into a chat transcript.
   @BotFather → `/revoke` → put the new one in `.env`. Do it before any public launch.
3. **Hand-verification** — open `content/out/verify.html` (regenerate with
   `content/verify_sample.py`) beside the PDF and tick off the 30 random + 24
   high-risk rows. **Do this before generating explanations**, or a parsing error
   gets baked into content you then pay a human to review.
4. **Choose a cluster strategy** — see §2. My recommendation: `--strategy figure`.
   Nothing has been written to the database yet.
5. **Tribute**: creator verification, both products created, API key + webhook
   secret. And ❗ **the merchant-of-record / EU VAT question in writing** (plan §4.1).
6. **Commercialista consultation** (plan §5). Still an M0 blocker for payments.

---

## 5. Next steps, in order

1. **Verify the bank** (§4.3 above) — 30 minutes, gates everything downstream.
2. **`python content/cluster.py --strategy figure --write`** once you've looked at
   `content/out/clusters.html` and agree the clusters are coherent.
   ⚠️ The largest clusters (50+) may be too coarse for one two-sentence
   explanation — the 53-member semaforo cluster spans both red-light and
   green-light rules. Consider splitting clusters above ~30 members.
3. **Codice della Strada ingestion** — pull D.Lgs. 285/1992 + DPR 495/1992 from
   Normattiva, split by article. Unblocked, mechanical, needed before generation.
4. **Topic → article map** (~1 afternoon, plan §3.3). I can draft it for you to correct.
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
