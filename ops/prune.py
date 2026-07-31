"""Which snapshots to keep.

Flat retention was the wrong shape. Keeping the last N at a fixed cadence means the
window is exactly N x interval and nothing older survives — at hourly with 56 kept, that
is two and a half days. A bad migration or a slow corruption discovered on Wednesday
about something that happened last month is then unrecoverable, and "we have backups" was
true the whole time.

Tiered keeps a dense recent window and a thin long tail: every hour for two days, one a
day for a month, one a week for a quarter, one a month for a year. That is ~90 files
instead of 56 and covers 12 months instead of 2 days, for about 240 MB at the current
2.7 MB per snapshot.

Written as a pure function over timestamps so it can be tested without touching a disk —
a pruner is the one piece of a backup system whose bugs delete things, and the only way
to be confident about it is to be able to run it a thousand times in a unit test.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

# Bucket sizes. Deliberately generous at the top: hourly snapshots are what you want
# during an incident, and they are the cheapest to keep.
HOURLY = 48      # two days
DAILY = 30       # a month
WEEKLY = 13      # a quarter
MONTHLY = 12     # a year

STAMP = re.compile(r"patente-(\d{8})-(\d{6})\.db$")


def parse_stamp(name: str) -> datetime | None:
    """The timestamp is read from the FILENAME, never from mtime.

    mtime happens to be right today because `docker cp` sets it, but it is metadata that
    a copy, a restore or an rsync would silently rewrite — and a pruner that sorts by it
    would then delete the wrong files while reporting success.
    """
    match = STAMP.search(name)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1) + match.group(2), "%Y%m%d%H%M%S")
    except ValueError:
        return None


def keepers(
    stamps: list[datetime],
    hourly: int = HOURLY,
    daily: int = DAILY,
    weekly: int = WEEKLY,
    monthly: int = MONTHLY,
) -> set[datetime]:
    """The set to KEEP. Everything else is a candidate for deletion.

    Walks newest-first and keeps the first snapshot seen in each period. Newest-first
    matters: it means the survivor of a given day is the LAST one taken that day, so a
    daily snapshot is a full day's work rather than whatever happened at 00:00.
    """
    ordered = sorted(set(stamps), reverse=True)
    keep: set[datetime] = set(ordered[:hourly])

    for period, limit in (
        ("%Y-%m-%d", daily),
        ("%G-W%V", weekly),
        ("%Y-%m", monthly),
    ):
        seen: set[str] = set()
        for stamp in ordered:
            key = stamp.strftime(period)
            if key in seen:
                continue
            seen.add(key)
            keep.add(stamp)
            if len(seen) >= limit:
                break
    return keep


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dir", type=Path, required=True)
    ap.add_argument("--apply", action="store_true",
                    help="actually delete; without it, only report")
    args = ap.parse_args()

    files = {}
    for path in args.dir.glob("patente-*.db"):
        stamp = parse_stamp(path.name)
        if stamp:
            files[stamp] = path

    if not files:
        print("no snapshots found")
        return 0

    keep = keepers(list(files))
    drop = sorted(set(files) - keep)

    print(f"{len(files)} snapshots, keeping {len(keep)}, dropping {len(drop)}")
    for stamp in drop:
        print(f"  {'delete' if args.apply else 'would delete'} {files[stamp].name}")
        if args.apply:
            files[stamp].unlink()
    return 0


if __name__ == "__main__":
    sys.exit(main())
