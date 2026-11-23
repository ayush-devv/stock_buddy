"""The writable database (pi.duckdb) and its read-only snapshot (ADR-0017).

- Only the evening run writes pi.duckdb.
- Each module owns its tables and declares them as ordered migration steps. `migrate`
  applies the steps a database hasn't seen yet, one transaction per step, and records the
  version as `schema:<component>` in system_state. Never edit a released step; append a
  new one (for example an ALTER TABLE ... ADD COLUMN).
- After a run, `export_snapshot` checkpoints, closes and copies the file to
  pi_read.duckdb via a temp file and an atomic rename, so the UI and `pi status` never
  hold a lock the writer needs.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

import duckdb

from pi.store.clock import require_aware, utc_now

SYSTEM_STATE_DDL = """CREATE TABLE IF NOT EXISTS system_state (
        key VARCHAR PRIMARY KEY,
        value VARCHAR,
        updated_at TIMESTAMPTZ NOT NULL
    )"""

CORE_DDL = (
    SYSTEM_STATE_DDL,
    """CREATE TABLE IF NOT EXISTS run_log (
        run_id VARCHAR NOT NULL,
        trade_date DATE,
        started_at TIMESTAMPTZ NOT NULL,
        finished_at TIMESTAMPTZ NOT NULL,
        status VARCHAR NOT NULL,
        detail VARCHAR
    )""",
)


def connect(path: Path, *, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(str(path), read_only=read_only)
    con.execute("SET TimeZone = 'UTC'")
    return con


CORE_MIGRATIONS: tuple[tuple[str, ...], ...] = (CORE_DDL,)


class SchemaError(Exception):
    pass


def migrate(
    con: duckdb.DuckDBPyConnection,
    component: str,
    steps: Sequence[Sequence[str]],
    now: datetime | None = None,
) -> int:
    """Apply the migration steps this database hasn't seen. Returns the new version."""
    con.execute(SYSTEM_STATE_DDL)
    key = f"schema:{component}"
    current = int(get_state(con, key) or 0)
    if current > len(steps):
        raise SchemaError(
            f"{component} schema is at version {current} but this code knows only "
            f"{len(steps)}; the database was written by newer code"
        )
    for version in range(current, len(steps)):
        con.begin()
        try:
            for statement in steps[version]:
                con.execute(statement)
            set_state(con, key, str(version + 1), now)
            con.commit()
        except Exception:
            con.rollback()
            raise
    return len(steps)


def get_state(con: duckdb.DuckDBPyConnection, key: str) -> str | None:
    row = con.execute("SELECT value FROM system_state WHERE key = ?", [key]).fetchone()
    return None if row is None else row[0]


def set_state(
    con: duckdb.DuckDBPyConnection, key: str, value: str | None, now: datetime | None = None
) -> None:
    now = require_aware(now or utc_now())
    con.execute(
        "INSERT OR REPLACE INTO system_state (key, value, updated_at) VALUES (?, ?, ?)",
        [key, value, now],
    )


def export_snapshot(db_path: Path, snapshot_path: Path) -> None:
    """Copy a *closed* database to the snapshot path atomically.

    The caller must have run CHECKPOINT and closed every connection to db_path first;
    copying an open file risks a torn copy because of the write-ahead log.
    """
    wal = db_path.with_name(db_path.name + ".wal")
    if wal.exists() and wal.stat().st_size > 0:
        raise RuntimeError(f"{wal} is not empty: checkpoint and close before exporting")
    fd, tmp_name = tempfile.mkstemp(dir=snapshot_path.parent, suffix=".tmp")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        shutil.copyfile(db_path, tmp)
        os.replace(tmp, snapshot_path)
    finally:
        tmp.unlink(missing_ok=True)
