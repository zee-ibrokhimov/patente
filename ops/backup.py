"""
backup.py — take a consistent, verified snapshot of the database.

Plan §6.4 calls the database the only irreplaceable data in the system, and it has grown
a second kind of irreplaceable since: user progress and entitlement cannot be
reconstructed at all, and the generated explanations and translations cost real money to
produce. Nothing was protecting either.

WHY NOT JUST COPY THE FILE
--------------------------
Because `patente.db` is not the database. WAL mode means the database is three files —
`.db`, `.db-wal`, `.db-shm` — and at the time of writing the WAL held 1.7 MB of committed
transactions that were not in the `.db` file at all. So:

  · copying `patente.db` alone silently loses everything since the last checkpoint;
  · copying all three while a writer is running captures them at different instants,
    which is a torn set and can restore as a corrupt database.

`sqlite3.Connection.backup()` is the online backup API. It takes a single consistent
snapshot into one file while the API keeps serving, which is the only correct way to do
this without stopping the service.

**This repo lives inside OneDrive, and that is not a backup.** OneDrive is continuously
syncing a moving three-file set; what it holds at any instant may be torn. Writing
verified snapshots into a folder OneDrive syncs *does* give the off-box copy §6.4 asks
for — the snapshot is consistent before OneDrive ever sees it. Point `--dest` outside
OneDrive as well if you want to survive losing the account.

A BACKUP YOU HAVE NOT VERIFIED IS NOT A BACKUP
----------------------------------------------
Every snapshot is opened, integrity-checked, and its row counts compared against the
source before it is kept. A backup that restores to a corrupt file is worse than none,
because it is discovered on the day it is needed.

Usage:
    python ops/backup.py                       # snapshot, verify, rotate
    python ops/backup.py --keep 30
    python ops/backup.py --dest D:/backups
    python ops/backup.py --verify backups/patente-20260730-1400.db
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.config import ROOT  # noqa: E402
from shared.db import sync_url  # noqa: E402

DEFAULT_DEST = ROOT / "backups"

# Counted on both sides of every snapshot. Progress and users are unreconstructable;
# explanations and translations were paid for. A mismatch in any of them means the copy is
# not the database.
CHECKED_TABLES = (
    "users", "progress", "purchases", "events",
    "questions", "clusters", "explanations", "translations",
)


def source_path() -> Path:
    """The database file behind `settings.database_url`."""
    url = sync_url()
    if not url.startswith("sqlite"):
        raise SystemExit(f"ERROR: only SQLite is supported here, not {url!r}")
    return Path(url.split("///", 1)[1]).resolve()


def counts(connection: sqlite3.Connection) -> dict[str, int]:
    out: dict[str, int] = {}
    for table in CHECKED_TABLES:
        try:
            out[table] = connection.execute(f"select count(*) from {table}").fetchone()[0]
        except sqlite3.Error:
            # A table that does not exist yet in an older database is not a mismatch,
            # as long as it is absent on both sides.
            out[table] = -1
    return out


def verify(path: Path, expected: dict[str, int] | None = None) -> list[str]:
    """Integrity-check a snapshot and compare its contents. Returns problems, empty if ok."""
    problems: list[str] = []
    if not path.exists() or path.stat().st_size == 0:
        return [f"{path} is missing or empty"]

    # Read-only so verification can never be the thing that damages a backup.
    #
    # A badly damaged file makes sqlite raise rather than return a verdict — "database
    # disk image is malformed" comes out of `PRAGMA integrity_check` itself. Letting that
    # propagate would kill the backup script with a traceback at the one moment it has
    # something important to say, so the exception *is* the finding.
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return [f"cannot be opened: {type(exc).__name__} {exc}"]

    try:
        result = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            problems.append(f"integrity_check said {result!r}")
        if expected is not None:
            got = counts(connection)
            for table, want in expected.items():
                if got.get(table) != want:
                    problems.append(f"{table}: source has {want}, backup has {got.get(table)}")
    except sqlite3.Error as exc:
        problems.append(f"unreadable: {type(exc).__name__} {exc}")
    finally:
        connection.close()
    return problems


def snapshot(source: Path, dest_dir: Path) -> tuple[Path, dict[str, int]]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    target = dest_dir / f"patente-{stamp}.db"

    live = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    try:
        expected = counts(live)
        copy = sqlite3.connect(target)
        try:
            # The online backup API: one consistent snapshot, taken while the API is
            # still serving. Nothing here needs the service stopped.
            live.backup(copy)
        finally:
            copy.close()
    finally:
        live.close()
    return target, expected


def rotate(dest_dir: Path, keep: int) -> list[Path]:
    existing = sorted(dest_dir.glob("patente-*.db"))
    doomed = existing[:-keep] if keep > 0 and len(existing) > keep else []
    for path in doomed:
        path.unlink()
    return doomed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", type=Path, default=DEFAULT_DEST)
    ap.add_argument("--keep", type=int, default=14, help="snapshots to retain; 0 keeps all")
    ap.add_argument("--verify", type=Path, help="check an existing snapshot and exit")
    args = ap.parse_args()

    if args.verify:
        problems = verify(args.verify)
        for problem in problems:
            print(f"  PROBLEM: {problem}", file=sys.stderr)
        print(f"{args.verify}: {'ok' if not problems else 'FAILED'}")
        return 1 if problems else 0

    source = source_path()
    if not source.exists():
        print(f"ERROR: {source} does not exist", file=sys.stderr)
        return 2

    wal = source.with_name(source.name + "-wal")
    wal_size = wal.stat().st_size if wal.exists() else 0
    print(f"source : {source}  ({source.stat().st_size / 1e6:.1f} MB"
          f"{f', + {wal_size / 1e6:.1f} MB uncheckpointed WAL' if wal_size else ''})")

    target, expected = snapshot(source, args.dest)
    problems = verify(target, expected)
    if problems:
        # Keep the bad file for inspection rather than deleting the evidence, but make
        # very sure nobody mistakes it for a usable backup.
        broken = target.with_suffix(".db.FAILED")
        target.rename(broken)
        print(f"\nBACKUP FAILED — kept as {broken.name} for inspection:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1

    print(f"backup : {target}  ({target.stat().st_size / 1e6:.1f} MB)  verified")
    for table, n in expected.items():
        if n >= 0:
            print(f"    {table:<14} {n}")

    removed = rotate(args.dest, args.keep)
    if removed:
        print(f"\n  rotated out {len(removed)}: {', '.join(p.name for p in removed)}")
    kept = sorted(args.dest.glob("patente-*.db"))
    print(f"  {len(kept)} snapshot(s) retained in {args.dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
