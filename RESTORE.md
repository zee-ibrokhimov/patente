# Restoring the patente database

Written to be followed at 3am by someone who did not build this, on a laptop, possibly
without the repo. Every command is copy-pasteable and nothing assumes a working server.

## Objectives

| | |
|---|---|
| **RPO** — how much data a disaster costs | **1 hour.** Snapshots run hourly; the off-box copy goes up in the same run. |
| **RTO** — how long recovery takes | **~15 min** if the box is alive. **~4 h** if it is not (new host, redeploy, restore). |
| **Detection** | **≤ 100 min.** Watchdog runs hourly at :40 and alerts on Telegram if the newest snapshot is older than 150 min. |
| **Retention** | Hourly for 2 days, daily for a month, weekly for a quarter, monthly for a year. ~93 snapshots, ~250 MB, covering 333 days. |

**What is irreplaceable:** users, progress, entitlement, purchases, exam sessions, and the
explanations and translations that cost real money to generate. Everything else — 7106
questions, 3382 clusters, 409 figures — is rebuilt from the repo by the entrypoint.

## Where the backups are

| | |
|---|---|
| Live database | Docker volume `rboj5u2xk5dj4o4yto89ablh_patente-data`, file `/data/patente.db` |
| Local snapshots | `/var/backups/patente/patente-YYYYMMDD-HHMMSS.db` |
| Off-box | Cloudflare R2, `patente/db/<name>.db.gz.gpg` — gzipped and AES256-encrypted |
| Decryption key | `/etc/patente-backup.key` — **and nowhere else. See the warning below.** |
| Alert channel | Telegram, via the bot token in `/etc/patente-backup.env` |

> ⚠️ **The passphrase is not in this repo and must not be.** If it exists only on the box
> it protects, an off-box backup is not a backup. Put it in a password manager NOW —
> without it the R2 objects are noise.

---

## Scenario 1 — the database is corrupt, the box is fine

Most likely case. ~15 minutes.

```bash
# 1. Pick a snapshot. Newest first.
ls -1t /var/backups/patente/patente-*.db | head

# 2. Rehearse it BEFORE touching anything. This drives real application code against a
#    scratch copy: models load, recent migration columns select, and the bot's own
#    selection query runs. A snapshot taken before a migration passes integrity_check
#    happily and then dies on the first query touching a column added later.
CID=$(docker ps -q --filter "name=api-rboj5u2xk5dj4o4yto89ablh")
docker cp /var/backups/patente/patente-YYYYMMDD-HHMMSS.db "$CID:/tmp/candidate.db"
docker exec "$CID" python ops/restore.py --from /tmp/candidate.db --rehearse

# 3. Stop the writers. The API owns the database; the bot only talks to the API.
#    Do this in the Coolify UI: patente -> Stop.

# 4. Restore. --force because overwriting a good database with a stale snapshot is a
#    plausible 3am mistake and this file exists for 3am.
docker exec "$CID" python ops/restore.py \
  --from /tmp/candidate.db --to /data/patente.db --force

# 5. Start it again in Coolify, then confirm.
curl -s http://127.0.0.1:8000/health   # from inside the api container
```

---

## Scenario 2 — the volume is gone, the box is fine

The container is not running, so **there is nothing to `docker exec` into.** This is the
step that makes a 15-minute RTO fiction if it is not written down.

```bash
# Find the image the app last built.
docker images | grep rboj5u2xk5dj4o4yto89ablh | head -1

# Run a throwaway container with ONLY the volume mounted, and restore into it.
docker run --rm \
  -v rboj5u2xk5dj4o4yto89ablh_patente-data:/data \
  -v /var/backups/patente:/backups:ro \
  <image-from-above> \
  python ops/restore.py --from /backups/patente-YYYYMMDD-HHMMSS.db \
                        --to /data/patente.db --force

# Then redeploy in Coolify. The entrypoint runs alembic and re-seeds content, both
# idempotent — it will report "0 new, 3382 kept" rather than rebuilding.
```

---

## Scenario 3 — the box is gone

~4 hours, and most of it is waiting on a rebuild rather than on the data.

1. **Get the newest object out of R2.** Cloudflare dashboard → R2 → bucket → `patente/db/`
   → newest → Download. Or with credentials:

   ```bash
   curl -sS --aws-sigv4 "aws:amz:auto:s3" --user "$R2_KEY:$R2_SECRET" \
     -o snapshot.db.gz.gpg \
     "$R2_ENDPOINT/$R2_BUCKET/patente/db/patente-YYYYMMDD-HHMMSS.db.gz.gpg"
   ```

2. **Decrypt and decompress.** This is where you find out whether the passphrase was
   saved anywhere other than the box that just died.

   ```bash
   gpg --batch --decrypt --passphrase-file /path/to/key \
       -o snapshot.db.gz snapshot.db.gz.gpg
   gunzip snapshot.db.gz
   sqlite3 snapshot.db "pragma integrity_check; select count(*) from users;"
   ```

3. **Rebuild the host** — new LXC, Docker, Coolify, then recreate the `patente` app from
   the GitHub repo. The compose file, Dockerfile and these scripts are all in git.

4. **Re-enter the environment variables.** `BOT_TOKEN_PROD`, `OPENAI_API_KEY`,
   `ADMIN_CHAT_IDS`, `SUPPORT_CONTACT`, `WEBAPP_URL`, and the `TRIBUTE_*` set once
   payments exist. **These are not in any backup** — deliberately, since a secrets blob in
   object storage is its own risk. Keep them in a password manager.

5. **Restore into the fresh volume** using Scenario 2's `docker run` form.

6. **Re-point the Cloudflare tunnel** at the new host, and check the bot is polling.

---

## Testing this

A restore procedure nobody has run is a belief, not a procedure. The hourly job already
rehearses every snapshot it takes, which covers scenario 1's data path — but it proves
nothing about gzip, gpg, the R2 credentials, or whether the object in the bucket is the
bytes you think it is.

**Do scenario 3's steps 1 and 2 by hand once a quarter**, against a real object, on a
machine that is not the server. It takes ten minutes and it is the only test of the whole
chain. Write the date here when you do:

- [ ] first off-box restore drill — date: ________
