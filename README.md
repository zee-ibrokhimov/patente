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
py -3.12 -m venv .venv          # any 3.12+; tested on 3.12 and 3.14
.venv/Scripts/python.exe -m pip install -e ".[content,dev]"
cp .env.example .env    # then fill in BOT_TOKEN_DEV
```

A venv is not portable between machines — `.venv/pyvenv.cfg` hardcodes the
interpreter path it was built from. Moving the repo (OneDrive, a new laptop)
means deleting `.venv` and redoing the two commands above, not repairing it.

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
.venv/Scripts/python.exe content/cluster.py --strategy figure --write
```

Groups statements into rule clusters. `--report` compares both strategies and
writes nothing; the cluster count is what sets the content budget, see
[STATUS.md](STATUS.md) §2. Clusters are keyed by what they are about rather than by
sort position, so `--write` is idempotent and a rerun preserves the explanations
already attached — see [STATUS.md](STATUS.md) §10 for why that matters.

```bash
.venv/Scripts/python.exe content/fetch_norms.py --source both
```

Pulls the Codice della Strada and its Regolamento from Normattiva into
`content/out/norms/`. Incremental — an interrupted run resumes. This is the
grounding text explanations are generated from. Roughly 20 minutes for both
statutes; run it in the foreground, it needs network.

```bash
.venv/Scripts/python.exe content/generate.py --topic "Segnali di precedenza" --dry-run
.venv/Scripts/python.exe content/generate.py --topic "Segnali di precedenza"
```

Writes the canonical Italian explanation for each cluster in one topic, grounded on
the articles `content/articles.py` maps that topic to. `--dry-run` prints the exact
prompt and spends nothing.

**The ministerial answer is withheld from the model and used to check it.** The model
decides VERO/FALSO from the article text alone; disagreement with the stored key
marks the cluster `flagged`, because it means either the explanation is wrong, the
topic is mapped to the wrong article, or the answer key itself is wrong. So are
explanations containing a number or a unit. Everything lands as `draft` or `flagged`
and a review sheet is written to `content/out/generate_report.csv` — only a human
sets `approved`, and only `approved` is ever served.

## Running

```bash
.venv/Scripts/python.exe -m uvicorn api.main:app --reload
```

`GET /health` reports whether content is loaded. Interactive docs at `/docs`.

## Backups

```bash
.venv/Scripts/python.exe ops/backup.py
```

Takes a consistent, verified snapshot into `backups/` while the API keeps serving, and
keeps the last 14.

**Do not back this up by copying `patente.db`.** In WAL mode the database is three files
and committed data sits in the `-wal` until a checkpoint moves it. Measured mid-session, a
plain copy of `patente.db` lost 167 rows — every translation and two thirds of the
explanations, the ones that cost money. `ops/backup.py` uses SQLite's online backup API,
which is the only way to get one consistent file without stopping the service.

Every snapshot is integrity-checked and its row counts compared against the source before
it is kept. A failure renames the file `.FAILED` and exits non-zero, so a scheduled task
reports it rather than looking like it worked. `--verify <file>` checks an existing one.

Being inside OneDrive is **not** a backup — it syncs a moving three-file set and what it
holds at any instant may be torn. Snapshots are consistent before OneDrive sees them, so
this does give the off-box copy plan §6.4 wants; pass `--dest` elsewhere too if losing the
account is worth surviving.

Run it daily at 03:00 (from an elevated prompt, one line):

```bash
schtasks /Create /TN patente-backup /SC DAILY /ST 03:00 /TR "'C:\Users\aibro\OneDrive\Desktop\patente_bot\.venv\Scripts\python.exe' 'C:\Users\aibro\OneDrive\Desktop\patente_bot\ops\backup.py'"
```

```bash
.venv/Scripts/python.exe ops/restore.py --rehearse
```

Restores the newest snapshot to a scratch copy and drives the **real application code**
against it — the models load, and the selection query the bot serves from actually runs.
Plan §12 asks for a restore to be tested *before* launch; opening the file and counting
rows is not that test. A snapshot taken before a migration passes `integrity_check` and
then dies on the first query touching a new column, which is exactly the failure a restore
must not be discovering live. Verified: dropping a migrated column makes the rehearsal
fail with "schema is behind the code".

To restore for real, deliberately:

```bash
.venv/Scripts/python.exe ops/restore.py --from backups/patente-20260730-120000.db --to patente.db --force
```

`--force` is required to overwrite, and any stale `-wal`/`-shm` beside the target are
removed first — a journal belonging to a different database is worse than none.

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
