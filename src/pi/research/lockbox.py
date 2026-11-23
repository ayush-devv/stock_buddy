"""The lockbox (architecture §14): the most recent ~2 years, held back for one final test.

Two things together make the lockbox:
1. **A date cutoff.** The live system still needs today's prices, which are exactly the
   lockbox dates, so they stay in `raw/`. The cutoff is enforced by the single research
   loader (`pi.research.loader`): no research read returns a row on or after
   `start`, and no sample is allowed whose 60-day label would touch it.
2. **A sealed copy** in data/lockbox/, written once by the backfill (WP 3.1). It is read
   only by the one-time gate evaluation that you run yourself (WP 3.7).

The seal is recorded here, in data/research.duckdb. No module outside pi.research may
import this one (import-linter contract); coding agents can't read data/lockbox/ (hook).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import duckdb

from pi.store.clock import require_aware

LOCKBOX_DIRNAME = "lockbox"

DDL = (
    """CREATE TABLE IF NOT EXISTS lockbox_seal (
        start_date DATE NOT NULL,
        content_hash VARCHAR NOT NULL,
        sealed_at TIMESTAMPTZ NOT NULL,
        burned_at TIMESTAMPTZ
    )""",
)


def lockbox_dir(data_dir: Path) -> Path:
    return data_dir / LOCKBOX_DIRNAME


def is_inside_lockbox(path: Path, data_dir: Path) -> bool:
    """True if `path` resolves to somewhere under the lockbox directory."""
    return path.resolve().is_relative_to(lockbox_dir(data_dir).resolve())


@dataclass(frozen=True)
class Seal:
    start: date
    content_hash: str
    sealed_at: datetime
    burned: bool


def current_seal(con: duckdb.DuckDBPyConnection) -> Seal | None:
    """The active (latest) seal, or None if no lockbox has been sealed yet."""
    row = con.execute(
        "SELECT start_date, content_hash, sealed_at, burned_at FROM lockbox_seal "
        "ORDER BY sealed_at DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    return Seal(row[0], row[1], require_aware(row[2]), row[3] is not None)


def record_seal(
    con: duckdb.DuckDBPyConnection, start: date, content_hash: str, now: datetime
) -> Seal:
    """Called once by the backfill loader (WP 3.1) after writing the sealed copy."""
    existing = current_seal(con)
    if existing is not None and not existing.burned:
        raise RuntimeError("a lockbox is already sealed and unused; it can't be replaced")
    con.execute(
        "INSERT INTO lockbox_seal VALUES (?, ?, ?, NULL)", [start, content_hash, require_aware(now)]
    )
    seal = current_seal(con)
    assert seal is not None
    return seal
