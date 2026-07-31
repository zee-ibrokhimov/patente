"""The pruner.

This is the one component of the backup system whose bugs DELETE things, so it is a pure
function over timestamps and gets tested harder than anything it protects.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from ops.prune import DAILY, HOURLY, MONTHLY, WEEKLY, keepers, parse_stamp


def series(count: int, step: timedelta, end: datetime | None = None) -> list[datetime]:
    end = end or datetime(2026, 7, 31, 12, 0, 0)
    return [end - step * i for i in range(count)]


# --- the filename is the authority ----------------------------------------

def test_the_timestamp_comes_from_the_name_not_the_file():
    """mtime is right today only because docker cp sets it. A copy, a restore or an
    rsync rewrites it, and a pruner sorting by mtime then deletes the wrong files while
    reporting success."""
    assert parse_stamp("patente-20260731-054412.db") == datetime(2026, 7, 31, 5, 44, 12)
    assert parse_stamp("patente-20260731-054412.db.SUSPECT") is None
    assert parse_stamp("something-else.db") is None
    assert parse_stamp("patente-99999999-999999.db") is None


# --- the dense recent window ------------------------------------------------

def test_everything_inside_the_hourly_window_is_kept():
    stamps = series(HOURLY, timedelta(hours=1))
    assert keepers(stamps) == set(stamps)


def test_hourly_snapshots_beyond_the_window_thin_out_but_a_daily_survives():
    stamps = series(24 * 10, timedelta(hours=1))
    keep = keepers(stamps)
    assert len(keep) < len(stamps), "nothing was pruned"
    # Every day in the range must still have at least one survivor.
    days = {s.date() for s in stamps}
    kept_days = {s.date() for s in keep}
    assert days == kept_days


# --- the long tail ----------------------------------------------------------

def test_a_year_of_dailies_leaves_a_monthly_trail():
    stamps = series(365, timedelta(days=1))
    keep = keepers(stamps)
    months = {s.strftime("%Y-%m") for s in keep}
    assert len(months) >= 12, f"only {len(months)} months survived a year"
    assert len(keep) < 120, f"kept {len(keep)}, expected roughly 90"


def test_the_survivor_of_a_day_is_the_last_snapshot_of_that_day():
    """Newest-first, so a daily snapshot is a full day's work rather than whatever
    happened to exist at 00:00."""
    day = datetime(2026, 5, 4)
    stamps = [day + timedelta(hours=h) for h in (1, 9, 17, 23)]
    stamps += series(HOURLY + 5, timedelta(hours=1))  # push them out of the hourly window
    keep = keepers(stamps)
    survivors = [s for s in keep if s.date() == day.date()]
    assert survivors == [day + timedelta(hours=23)]


# --- the properties that stop it eating everything --------------------------

def test_it_never_returns_an_empty_set_for_a_non_empty_input():
    for n in (1, 2, 5, 50, 500):
        assert keepers(series(n, timedelta(hours=1))), f"pruned everything at n={n}"


def test_the_newest_snapshot_is_always_kept():
    """The single most important property. Whatever else it does, it must never delete
    the most recent good backup."""
    for step in (timedelta(minutes=30), timedelta(hours=6), timedelta(days=3)):
        stamps = series(200, step)
        assert max(stamps) in keepers(stamps)


def test_keeping_is_idempotent():
    """Running the pruner twice must not keep eating: the second pass over an
    already-pruned set should change nothing."""
    stamps = series(400, timedelta(hours=1))
    once = keepers(stamps)
    twice = keepers(list(once))
    assert twice == once


def test_duplicates_do_not_inflate_the_count():
    stamps = series(60, timedelta(hours=1))
    assert keepers(stamps + stamps) == keepers(stamps)


def test_an_empty_input_is_not_an_error():
    assert keepers([]) == set()


@pytest.mark.parametrize("n", [1, 7, 49, 100, 1000])
def test_it_always_keeps_at_least_the_configured_recent_window(n):
    stamps = series(n, timedelta(hours=1))
    assert len(keepers(stamps)) >= min(n, HOURLY)


def test_the_tiers_are_sane():
    assert HOURLY >= 24 and DAILY >= 7 and WEEKLY >= 4 and MONTHLY >= 12
