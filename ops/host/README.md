# Host-side backup tooling

These files run on the host, not in a container, because the database lives in a Docker
volume and the snapshot has to be taken from inside the running API container and copied
out. They are versioned here because a recovery procedure that exists only on the box it
protects is not a recovery procedure.

## Install

    sudo install -m 755 ops/host/patente-backup /usr/local/sbin/patente-backup
    sudo install -m 755 ops/host/patente-backup-watchdog /usr/local/sbin/patente-backup-watchdog
    sudo install -m 644 ops/host/cron.d-patente /etc/cron.d/patente
    sudo systemctl restart cron

Then create `/etc/patente-backup.env` (mode 600, NOT in this repo — it holds a token):

    TG_TOKEN=<the bot token>
    TG_CHAT=<your numeric telegram id>

Without it the backup still runs; it just cannot alert.

For off-box replication, add to the same file:

    R2_KEY=<access key id>
    R2_SECRET=<secret access key>
    R2_ENDPOINT=https://<account-id>.r2.cloudflarestorage.com
    R2_BUCKET=zeehub-backups
    BACKUP_KEYFILE=/etc/patente-backup.key

and create the passphrase file:

    head -c 48 /dev/urandom | base64 | sudo tee /etc/patente-backup.key
    sudo chmod 600 /etc/patente-backup.key

**Then put that passphrase in a password manager.** A key that exists only on the box it
protects makes the off-box copy unreadable in the one scenario it exists for.

With no R2 credentials the backup still runs and still succeeds — it just stays local, and
the watchdog says so.

## What the guard is for

`ops/backup.py` proves a snapshot is a *valid SQLite database*. It cannot prove it is
*this product's production database*. A freshly seeded database has 7106 questions and
3382 clusters and restores perfectly — and that is exactly what a backup pointed at the
wrong file looks like.

The distinguishing signal is `events`. STATUS.md §3 establishes that GDPR erasure
anonymises event rows rather than deleting them, so the table is append-only and its
count can only grow. If it shrinks, this is either a different database or real data
loss. Either way the script quarantines the snapshot, **skips pruning** and alerts —
because 56 successful backups of the wrong database would otherwise delete every good
snapshot within 14 days, which is the automated version of an incident this project has
already had once.

`.manifest.json` in the backup directory carries the last accepted counts.

## What the watchdog is for

`patente-backup` alerts when it fails. It cannot alert when it never runs at all — cron
dead, docker dead, box off, disk full, script deleted. That is not hypothetical: it is
the state this deployment was in for its first hours.

So `patente-backup-watchdog` is a SEPARATE file on a SEPARATE cron entry, and it asserts
outcomes rather than trusting exit codes: a local snapshot newer than 150 minutes, an
off-box copy of the same age once R2 is configured, no quarantined `.SUSPECT` files, and
enough disk for the next run. It stays silent when healthy — an alert that fires on a
known, accepted gap trains you to ignore alerts.

## Known gaps

- **No external dead-man's switch.** Both scripts run on the box they watch, so a
  complete host failure is silent until someone notices. A free healthchecks.io ping, or
  a Cloudflare Worker HEADing the newest R2 object, would close it.
- **Secrets are not backed up.** The bot token, OpenAI key and Tribute config live only
  in Coolify's environment. Deliberate — a secrets blob in object storage is its own
  risk — but it means a host rebuild needs a password manager. See RESTORE.md.
