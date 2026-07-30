# Deployment prompt

Paste everything below the line into a Claude Code session running **on the server**.

Fill in the two placeholders first: `<SERVER_DOMAIN>` and where you want the app to live.

---

I need you to deploy a Telegram bot to this server. Read this whole brief before starting,
then tell me your plan and what you need from me before you change anything.

## What it is

A Telegram bot that drills the official Italian driving-theory question bank. Two
long-running processes plus a SQLite database:

- **the API** — FastAPI, owns all business logic and the database
- **the bot** — aiogram, long-polling, a thin HTTP client of the API

Source: `https://github.com/zee-ibrokhimov/patente` (branch `master`, public).
Read `README.md` and `STATUS.md` in the repo first — STATUS.md is a detailed handover and
explains most of what follows.

## ⚠️ Security constraint — read this before exposing anything

**The API has no authentication whatsoever.** There is no token, no session, no
`Authorization` header anywhere in `api/`. Any caller that can reach it can:

- `POST /users/{chat_id}/pass` — grant themselves an unlimited paid pass
- `GET /users/{chat_id}` — read any user
- `DELETE /users/{chat_id}` — delete any user and their progress

This is deliberate: the API was built as an internal service on localhost with the bot as
its only client. It is safe there and unsafe anywhere else.

So:

1. **Bind the API to localhost** (or a private network the bot alone can reach). It must
   not be reachable from the internet.
2. **Exactly one path must be publicly reachable: `POST /webhooks/tribute`.** The payment
   provider calls it. It is the only endpoint that authenticates its caller, via an
   HMAC-SHA256 signature over the raw request body, and it fails closed if the secret is
   unset. Expose that path and nothing else — a reverse proxy with a single location block,
   not a blanket proxy.
3. If you cannot cleanly expose just that one path, tell me rather than exposing the whole
   API. Payments are not live yet, so we can ship without the webhook and add it later.

## Runtime

- **Python 3.12 or newer.** Tested on 3.12 and 3.14.
- Install with `pip install -e ".[content,dev]"` from the repo root. Drop `dev` if you
  prefer, but keep `content` — `cluster.py` needs it if you ever reseed.
- `pyproject.toml` already declares an explicit package list; if setuptools complains about
  "multiple top-level packages", you are on an old checkout.
- No Docker requirement. systemd units are fine and probably simpler here. If you prefer
  containers, note the database must be on a **persistent volume** — losing it loses every
  user's progress and paid content.

## Secrets — I will provide these, do not invent them

Create `.env` in the repo root from `.env.example`. Never commit it. I will give you:

- `BOT_TOKEN_PROD` — **a different bot token from the one I use locally.** See below.
- `OPENAI_API_KEY`
- `ADMIN_CHAT_IDS` — comma-separated Telegram ids allowed to run `/grant`
- `TRIBUTE_WEBHOOK_SECRET`, `TRIBUTE_API_KEY`, `TRIBUTE_PRODUCT_1M`, `TRIBUTE_PRODUCT_3M`
  — only if we are wiring payments now; leave blank otherwise and the webhook safely
  refuses everything.

Also set:

- `ENV=prod` — this is what makes the app use `BOT_TOKEN_PROD` rather than the dev token.
- `API_BASE_URL` — the bot's view of the API, e.g. `http://127.0.0.1:8000`.
- `DATABASE_URL` — e.g. `sqlite+aiosqlite:////srv/patente/patente.db`. Note **four**
  slashes for an absolute path.

**Config precedence gotcha:** the app uses pydantic-settings, which reads real environment
variables *in preference to* `.env`. That is correct for production — but it means a stale
exported variable silently overrides the file and is very hard to spot. It has already cost
us an hour once. If a value seems ignored, check the process environment first.

## ⚠️ Two bots, or you will get a polling conflict

Telegram allows only one `getUpdates` consumer per token. I run a bot locally with
`BOT_TOKEN_DEV`. If the server polls the **same** token, the two will fight and updates will
be delivered to one or the other at random.

Use a **separate bot** created via @BotFather for production, and put its token in
`BOT_TOKEN_PROD` with `ENV=prod`. Do not reuse the dev token.

## The database — restore, don't reseed

The repo ships everything needed to build the database from scratch, but **please do not**,
unless I say so. It already contains generated explanations and translations that cost real
money to produce, and reseeding would not recreate them.

Preferred: I will give you a **verified snapshot** produced by `ops/backup.py`
(`backups/patente-YYYYMMDD-HHMMSS.db`). Then:

```bash
python ops/restore.py --rehearse --from <snapshot>     # proves it works before you use it
python ops/restore.py --from <snapshot> --to /srv/patente/patente.db --force
python -m alembic upgrade head                          # in case the code is ahead
```

`--rehearse` restores to a scratch copy and runs the real application queries against it. If
it reports "schema is behind the code", the snapshot predates a migration — tell me, do not
work around it.

Only if I explicitly ask for a fresh build:

```bash
python -m alembic upgrade head
python content/seed.py            # reads the committed content/out/questions.json
python content/cluster.py --strategy figure --write
```

The source PDF is **not** in the repo (gitignored, 24 MB), but `questions.json` and all 409
figures are, so seeding works without it. `content/extract.py` will not run and does not
need to.

## Running the two processes

```bash
python -m uvicorn api.main:app --host 127.0.0.1 --port 8000
python -m bot.main
```

Both should restart on failure and start on boot. The bot depends on the API being up;
have it retry rather than die.

**Do not run more than one API process** (no `--workers 4`). SQLite plus a per-cluster
in-process lock that dedupes concurrent OpenAI calls both assume a single process. Multiple
workers would mean duplicate paid API calls and write contention. One worker is ample —
this is a long-polling bot, not a high-traffic web app.

## Backups

`ops/backup.py` takes a consistent, verified snapshot while the service keeps running, and
keeps the last 14. **Do not back up by copying `patente.db`** — in WAL mode the database is
three files and a plain copy measurably lost 167 rows in testing, including every
translation. The script uses SQLite's online backup API instead.

Please add a daily cron job, and put the snapshots somewhere off-box:

```bash
0 3 * * * cd /srv/patente && /srv/patente/.venv/bin/python ops/backup.py --dest /var/backups/patente
```

It exits non-zero if the snapshot fails verification, so cron will report it.

## Done means

Tell me you are finished only when all of these hold, and show me the output:

1. `curl http://127.0.0.1:8000/health` returns `{"status":"ok", ..., "questions":7106, "seeded":true}`
2. `curl https://<SERVER_DOMAIN>/health` **fails or is refused** — the API must not be public
3. `curl -X POST https://<SERVER_DOMAIN>/webhooks/tribute -d '{}'` returns **400**, not 404
   and not 200 — proving the one public path is routed and failing closed
4. The bot logs `Run polling for bot @<name>` and stays up
5. Sending `/start` then `/quiz` in Telegram returns a question
6. Both services survive `reboot`
7. `python ops/backup.py` produces a verified snapshot, and `ops/restore.py --rehearse`
   passes on it

## Things that will bite you

- **294 tests pass** on a correct checkout: `python -m pytest -q`. Run them after install —
  they are fast and they catch a broken environment immediately.
- The app writes generated content to the database at runtime, so the filesystem holding it
  must be writable and must not be a container layer that disappears on redeploy.
- `content/out/` is committed and read at runtime: the legal corpus lives in
  `content/out/norms/*.json` and the figures in `content/out/images/`. Do not prune them as
  "build artefacts" — explanation generation reads the corpus, and the bot serves the images.
- Log to somewhere I can read. The interesting failures are OpenAI errors during background
  generation and any `database is locked`.

Ask me anything that is ambiguous rather than guessing, especially about the domain,
the reverse proxy, and which secrets to use.
