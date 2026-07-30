"""Database snapshots.

The property that matters is the one a plain file copy does not have: in WAL mode the
database is three files, and committed data lives in the `-wal` until a checkpoint moves
it. Measured on the real database mid-session, `cp patente.db` lost 167 rows — every
translation and two thirds of the explanations, the ones that cost money.

The second property is that a snapshot is verified before it is trusted. A backup
discovered to be corrupt on the day it is needed is worse than knowing you have none.
"""

from __future__ import annotations

import sqlite3

import pytest

from ops import backup


def make_db(path, rows: int = 50, checkpoint: bool = False) -> dict[str, int]:
    """A WAL-mode database with committed rows still sitting in the -wal file."""
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("create table users (chat_id integer primary key)")
    connection.execute("create table explanations (id integer primary key, text text)")
    connection.executemany(
        "insert into explanations (text) values (?)", [(f"row {i}",) for i in range(rows)]
    )
    connection.execute("insert into users values (1)")
    connection.commit()
    if checkpoint:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    # Left open deliberately: a backup must work while something is still connected.
    return connection


def count(path, table="explanations") -> int:
    connection = sqlite3.connect(f"file:{Pathlike(path)}?mode=ro", uri=True)
    try:
        return connection.execute(f"select count(*) from {table}").fetchone()[0]
    finally:
        connection.close()


def Pathlike(path) -> str:
    from pathlib import Path

    return Path(path).as_posix()


# --- the property a file copy does not have ---------------------------------

def test_a_snapshot_includes_data_still_in_the_wal(tmp_path):
    source = tmp_path / "live.db"
    live = make_db(source, rows=50)
    try:
        assert (source.with_name("live.db-wal")).stat().st_size > 0, "no WAL to test with"

        # What a plain copy gets. On the real database this lost 167 rows; here it does
        # not even get the schema, because the CREATE TABLE is in the WAL as well.
        naive = tmp_path / "naive.db"
        naive.write_bytes(source.read_bytes())

        target, expected = backup.snapshot(source, tmp_path / "out")
        assert not backup.verify(target, expected)
        assert count(target) == 50

        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            count(naive)
    finally:
        live.close()


def test_a_snapshot_works_while_a_writer_holds_the_database(tmp_path):
    """The service does not get stopped to take a backup."""
    source = tmp_path / "live.db"
    live = make_db(source, rows=10)
    try:
        live.execute("insert into explanations (text) values ('during backup')")
        live.commit()
        target, expected = backup.snapshot(source, tmp_path / "out")
        assert not backup.verify(target, expected)
        assert count(target) == 11
    finally:
        live.close()


# --- verification -----------------------------------------------------------

def test_verify_rejects_a_truncated_file(tmp_path):
    source = tmp_path / "live.db"
    live = make_db(source, rows=20)
    try:
        target, expected = backup.snapshot(source, tmp_path / "out")
    finally:
        live.close()

    target.write_bytes(target.read_bytes()[: len(target.read_bytes()) // 2])
    assert backup.verify(target, expected), "a half-file must not pass verification"


def test_verify_rejects_a_row_count_mismatch(tmp_path):
    source = tmp_path / "live.db"
    live = make_db(source, rows=20)
    try:
        target, expected = backup.snapshot(source, tmp_path / "out")
    finally:
        live.close()

    problems = backup.verify(target, {**expected, "explanations": 999})
    assert any("explanations" in p for p in problems)


def test_verify_rejects_a_missing_file(tmp_path):
    assert backup.verify(tmp_path / "nope.db", None)


def test_a_table_absent_from_both_sides_is_not_a_mismatch(tmp_path):
    """An older database missing a table added by a later migration is fine, as long as
    the snapshot matches it."""
    source = tmp_path / "live.db"
    live = make_db(source, rows=5)
    try:
        target, expected = backup.snapshot(source, tmp_path / "out")
    finally:
        live.close()
    assert expected["progress"] == -1        # never existed here
    assert not backup.verify(target, expected)


# --- rotation ---------------------------------------------------------------

def test_rotation_keeps_the_newest_and_drops_the_rest(tmp_path):
    for stamp in range(1, 6):
        (tmp_path / f"patente-2026073{stamp}-000000.db").write_text("x")
    removed = backup.rotate(tmp_path, keep=2)
    kept = sorted(p.name for p in tmp_path.glob("patente-*.db"))
    assert len(removed) == 3
    assert kept == ["patente-20260734-000000.db", "patente-20260735-000000.db"]


def test_keep_zero_retains_everything(tmp_path):
    for stamp in range(1, 4):
        (tmp_path / f"patente-2026073{stamp}-000000.db").write_text("x")
    assert backup.rotate(tmp_path, keep=0) == []
    assert len(list(tmp_path.glob("patente-*.db"))) == 3


def test_rotation_ignores_files_it_did_not_write(tmp_path):
    (tmp_path / "patente-20260730-000000.db").write_text("x")
    (tmp_path / "something-else.db").write_text("x")
    (tmp_path / "patente-20260730-000000.db.FAILED").write_text("x")
    backup.rotate(tmp_path, keep=0)
    assert (tmp_path / "something-else.db").exists()
    assert (tmp_path / "patente-20260730-000000.db.FAILED").exists()


@pytest.mark.parametrize("keep", [1, 3, 10])
def test_rotation_never_removes_the_most_recent(tmp_path, keep):
    names = [f"patente-2026073{i}-000000.db" for i in range(1, 6)]
    for name in names:
        (tmp_path / name).write_text("x")
    backup.rotate(tmp_path, keep=keep)
    assert (tmp_path / names[-1]).exists()
