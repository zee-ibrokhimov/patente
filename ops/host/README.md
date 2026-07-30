# Host-side backup tooling

These files run on the host, not in a container, because the database lives in a Docker
volume and the snapshot has to be taken from inside the running API container and copied
out. They are versioned here because a recovery procedure that exists only on the box it
protects is not a recovery procedure.

## Install

    sudo install -m 755 ops/host/patente-backup /usr/local/sbin/patente-backup
    sudo install -m 644 ops/host/cron.d-patente /etc/cron.d/patente
    sudo systemctl restart cron

Then create `/etc/patente-backup.env` (mode 600, NOT in this repo — it holds a token):

    TG_TOKEN=<the bot token>
    TG_CHAT=<your numeric telegram id>

Without it the backup still runs; it just cannot alert.

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

## Known gaps

- **Still on-box.** `/var/backups` and `/var/lib/docker/volumes` are the same device
  (`stat -c %d` → 64518 for both), so there is one copy of the data on one disk. Off-box
  replication to R2 is the next step and needs a bucket plus a scoped token.
- **No dead-man's switch.** The script alerts when it fails. It cannot alert when it
  never runs — cron dead, docker dead, box off.
