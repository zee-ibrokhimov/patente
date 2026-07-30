# How this is deployed

**Live deployment: Coolify.** Project `patente`, app uuid `rboj5u2xk5dj4o4yto89ablh`,
three containers built from `docker-compose.yaml`:

| service | what it is | public? |
|---|---|---|
| `api` | FastAPI, owns all logic and the database | **no** — no domain, so no Traefik router exists |
| `bot` | aiogram long polling, companion surface | no |
| `web` | nginx: Mini App + the one proxied API prefix | **yes** — `patente.zeehub.xyz` |

The database is the Docker volume `rboj5u2xk5dj4o4yto89ablh_patente-data`. Everything
irreplaceable is in it: progress, entitlement, and the explanations and translations
that cost money to generate.

## Why the unauthenticated API is safe here

Every route under `/users/{chat_id}` takes identity from the URL, so any caller can
claim to be anyone. What keeps it safe is **not** the `internal` network — Coolify
attaches every service to its proxy network too, and a probe from there gets a 200.
It is safe because:

1. `api` has **no domain**, so Traefik generates no router and there is no path in
   from the edge. This is the load-bearing fact. Never set a domain on `api`.
2. `web`'s nginx proxies exactly one prefix, `/webapp/*`, whose routes take identity
   from a Telegram-signed `initData` blob instead of the URL. Every other prefix
   returns 404 explicitly (`webapp/nginx.conf`).

## Backups

`/usr/local/sbin/patente-backup`, daily at 03:00 via `/etc/cron.d/patente`. It
snapshots inside the container with SQLite's online backup API, **rehearses a restore
against the snapshot**, and only then copies it out to `/var/backups/patente`. Exits
non-zero on failure so cron reports it.

Never back up by copying `patente.db` — in WAL mode it is three files and a plain copy
lost 167 rows in testing, including every translation (STATUS.md §17).

`/var/backups/patente/legacy-systemd/` holds snapshots of the retired systemd
deployment's database. Different database — do not restore one over the live one.

**Still open: these are on-box.** STATUS.md §6.4 wants an off-box copy.

## The retired systemd deployment

`/srv/patente` was the first deployment (systemd units on port 8100). The units are
stopped and disabled; the directory stays because it holds the working git checkout
and the `.venv` used to run the test suite. Its `patente.db` is stale and unused.

To run the tests:

```bash
cd /srv/patente && setpriv --reuid=patente --regid=patente --init-groups \
  env OPENAI_API_KEY=dummy .venv/bin/python -m pytest -q
```

`OPENAI_API_KEY` must be non-empty or 17 tests fail on a guard before the mocked
client is reached. Any dummy value works; no real calls are made.

## Deploying a change

Push to `master`, then redeploy. Coolify's auto-deploy webhook does not work — GitHub
cannot reach Coolify, which sits behind Cloudflare Access — so the deploy is manual
from the Coolify UI or the API.
