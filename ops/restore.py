"""
restore.py — put a snapshot back, and rehearse doing so.

Plan §12: "Backups: automated, off-box, and **test a restore before launch**, not after."
The second half is the part everyone skips, and it is the half that matters — a backup is
a belief about the future until someone has restored one.

`--rehearse` is that test, and it is deliberately more than "the file opens". It restores a
snapshot to a scratch path and then drives the **real application code** against it: the
Alembic revision is checked, the models load, and the actual selection query the bot uses
to serve a question is run. A file can pass `PRAGMA integrity_check` and still be useless
because it predates a migration; only running the app against it proves otherwise.

Nothing here writes to the live database unless `--to` is given explicitly, and it refuses
to overwrite without `--force`. Restoring over a good database with a stale snapshot is a
plausible 3am mistake, and the whole point of this file is 3am.

Usage:
    python ops/restore.py --rehearse                 # newest snapshot, scratch copy
    python ops/restore.py --rehearse --from backups/patente-20260730-120000.db
    python ops/restore.py --from backups/... --to patente.db --force
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from api.models import Cluster, Explanation, Question, Topic, User  # noqa: E402
from ops.backup import DEFAULT_DEST, verify  # noqa: E402
from shared.db import make_sync_engine  # noqa: E402


def newest(dest_dir: Path) -> Path | None:
    snapshots = sorted(dest_dir.glob("patente-*.db"))
    return snapshots[-1] if snapshots else None


def rehearse(snapshot: Path) -> list[str]:
    """Restore to scratch and drive the real application code against it.

    Returns problems, empty if the snapshot would actually work as the live database.
    """
    problems = verify(snapshot)
    if problems:
        return problems

    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "restored.db"
        shutil.copy2(snapshot, target)
        # Built and disposed by hand rather than via `sync_session_factory`: the
        # connection pool keeps the file open, and Windows will not delete a file another
        # handle still holds. The scratch directory then fails to clean up and the
        # rehearsal ends in a traceback that reads like a failed restore when it succeeded.
        engine = make_sync_engine(f"sqlite:///{target.as_posix()}")
        try:
            problems.extend(probe(sessionmaker(engine, expire_on_commit=False)))
        finally:
            engine.dispose()
    return problems


def probe(factory) -> list[str]:
    """Drive the real application code against a restored file.

    Opening it and counting rows is not enough. A snapshot taken before a migration passes
    `integrity_check` and then fails on the first query touching a new column — which is
    exactly the failure a restore must not be discovering live.
    """
    problems: list[str] = []
    with factory() as session:
        for model, label in ((Question, "questions"), (Topic, "topics"), (Cluster, "clusters")):
            if not session.execute(select(func.count()).select_from(model)).scalar():
                problems.append(f"no {label} — this is not a seeded database")

        # The query the bot actually serves from, joins and all.
        try:
            served = session.execute(
                select(Question.id, Question.statement_it, Question.cluster_id)
                .join(Topic, Topic.id == Question.topic_id)
                .where(Question.cluster_id.is_not(None))
                .limit(1)
            ).first()
            if served is None:
                problems.append("no clustered question could be selected")
        except Exception as exc:  # noqa: BLE001
            problems.append(f"the selection query failed: {type(exc).__name__} {exc}")

        # The columns the most recent migrations added, which are precisely what an older
        # snapshot would be missing.
        try:
            session.execute(
                select(Explanation.status, Explanation.flags, Explanation.disputed).limit(1)
            ).first()
            session.execute(select(Cluster.natural_key).limit(1)).first()
            session.execute(select(User.pass_expires_at).limit(1)).first()
        except Exception as exc:  # noqa: BLE001
            problems.append(
                f"schema is behind the code — a migration is missing from this snapshot: "
                f"{type(exc).__name__} {exc}"
            )
    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="source", type=Path,
                    help="snapshot to use; defaults to the newest in backups/")
    ap.add_argument("--to", type=Path, help="restore here (omit for a rehearsal only)")
    ap.add_argument("--rehearse", action="store_true",
                    help="restore to scratch and run the app against it")
    ap.add_argument("--force", action="store_true", help="allow overwriting --to")
    args = ap.parse_args()

    snapshot = args.source or newest(DEFAULT_DEST)
    if snapshot is None:
        print(f"ERROR: no snapshots in {DEFAULT_DEST} — run ops/backup.py first",
              file=sys.stderr)
        return 2
    if not snapshot.exists():
        print(f"ERROR: {snapshot} does not exist", file=sys.stderr)
        return 2

    print(f"snapshot: {snapshot}  ({snapshot.stat().st_size / 1e6:.1f} MB)")

    if args.rehearse or not args.to:
        problems = rehearse(snapshot)
        if problems:
            print("\nREHEARSAL FAILED — this snapshot would not work as the live database:",
                  file=sys.stderr)
            for problem in problems:
                print(f"  {problem}", file=sys.stderr)
            return 1
        print("  rehearsal: restored to scratch, schema current, "
              "the bot's own selection query ran — this snapshot is usable")
        if not args.to:
            return 0

    target = args.to
    if target.exists() and not args.force:
        print(f"\nERROR: {target} exists. Restoring over a live database with a stale "
              f"snapshot is a 3am mistake worth making hard — pass --force if you mean it.",
              file=sys.stderr)
        return 2

    if target.exists():
        # The WAL and shm belong to the database being replaced; leaving them beside a
        # restored file makes SQLite try to replay a journal from a different database.
        for suffix in ("-wal", "-shm"):
            stale = target.with_name(target.name + suffix)
            if stale.exists():
                stale.unlink()
                print(f"  removed stale {stale.name}")

    shutil.copy2(snapshot, target)
    print(f"  restored -> {target}")
    if verify(target):
        print("  but it does not verify in place — investigate before starting anything",
              file=sys.stderr)
        return 1
    print("  verified. Restart the API and the bot.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
