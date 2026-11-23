"""The writable database (pi.duckdb) and its read-only snapshot (ADR-0017).

- Only the evening run writes pi.duckdb.
- Each module owns its tables and passes its DDL to `ensure_schema`.
- After a run, `export_snapshot` checkpoints, closes and copies the file to
  pi_read.duckdb via a temp file and an atomic rename, so the UI and `pi status` never
  hold a lock the writer needs.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

import duckdb

from pi.store.clock import require_aware, utc_now

CORE_DDL = (
    """CREATE TABLE IF NOT EXISTS system_state (
        key VARCHAR PRIMARY KEY,
        value VARCHAR,
        updated_at TIMESTAMPTZ NOT NULL
    )""",
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


def ensure_schema(con: duckdb.DuckDBPyConnection, ddl: Iterable[str]) -> None:
    for statement in ddl:
        con.execute(statement)


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
