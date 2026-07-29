# Patente Quiz Bot

Telegram bot drilling the official Italian driving-theory question bank (patente
AM/B) in exact ministerial Italian, with an optional native-language translation
underneath and the reasoning behind the correct answer after you reply.

See [patente-bot-plan.md](patente-bot-plan.md) for the product, pricing and
content plan. This file is just how to run it.

## Layout

```
api/          FastAPI — owns ALL business logic
  routes/     users, quiz
  services/   selection, leitner, entitlement, content, answers, stats, events
  models/     SQLAlchemy
  migrations/ Alembic
bot/          aiogram — thin client, HTTP calls to api/
webapp/       Mini App — thin client, HTTP calls to api/
content/      one-off pipeline, run manually
shared/       config, constants, db
tests/
```

**`bot/` and `webapp/` contain no business logic and no database access.** Both
call the API. That rule is the only thing keeping two frontends from becoming two
implementations of the Leitner rules that disagree with each other.

## Setup

```bash
py -3.12 -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[content,dev]"
cp .env.example .env    # then fill in BOT_TOKEN_DEV
```

## Content pipeline

Run manually, in order. Nothing here is part of the runtime service.

```bash
.venv/Scripts/python.exe content/extract.py --report
```

Parses the ministerial listato PDF into `content/out/questions.json` plus
deduplicated figures. Refuses to write if the answer census, the statement-number
census or the figure checks disagree with the source — see the module docstring
for the four ways text-flow parsing of this PDF is quietly wrong.

```bash
.venv/Scripts/python.exe content/verify_sample.py
```

Builds `content/out/verify.html`, the hand-verification sheet: 30 stratified
random rows plus rows drawn from the classes where a parser bug would hide.

```bash
.venv/Scripts/alembic.exe upgrade head
.venv/Scripts/python.exe content/seed.py --dry-run
.venv/Scripts/python.exe content/seed.py
```

Seeding is diff-based: a reissued listato only touches what changed.

```bash
.venv/Scripts/python.exe content/cluster.py --report --sample
```

Groups statements into rule clusters and compares both strategies without writing.
The cluster count is what sets the content budget — see [STATUS.md](STATUS.md) §2.

```bash
.venv/Scripts/python.exe content/fetch_norms.py --source both
```

Pulls the Codice della Strada and its Regolamento from Normattiva into
`content/out/norms/`. Incremental — an interrupted run resumes. This is the
grounding text explanations are generated from.

## Running

```bash
.venv/Scripts/python.exe -m uvicorn api.main:app --reload
```

`GET /health` reports whether content is loaded. Interactive docs at `/docs`.

## Tests

```bash
.venv/Scripts/python.exe -m pytest -q
```

Focused on the things that are silent when broken: statement/answer alignment,
entitlement, Leitner scheduling, reseed behaviour, and GDPR erasure.

## Conventions

- **Never trust the client for entitlement.** Locked content is absent from the
  response, not blanked in the frontend.
- **All timestamps are timezone-aware UTC.** Binding a naive datetime raises.
- **Two bot tokens**, dev and prod. Testing against the live bot while users are
  on it is a bad afternoon.
- Never commit `.env`, the Tribute API key, or the webhook secret.
