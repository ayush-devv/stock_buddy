"""The evening run (WP 1.4, architecture §9, ADR-0003).

- Only this process writes pi.duckdb, guarded by a lock file.
- It processes every pending trade date in order. Each date is one transaction and is
  idempotent: re-running a completed date changes nothing.
- An expected trading day with no bhavcopy pauses the run there; later dates wait.
- At the end it records the clean-day streak and any corrections that arrived for days
  already completed, checkpoints, closes, and exports the read-only snapshot.

Exit codes: 0 ok, 1 failed, 3 paused (missing data). The heartbeat (WP 1.5) pings only on 0.
"""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from uuid import uuid4

import duckdb

from pi.config import load_all
from pi.refdata import securities
from pi.refdata.calendar import clean_streak, missing_days, trading_days
from pi.store import db
from pi.store.clock import decision_cutoff, latest_due_trade_date, require_aware, utc_now
from pi.store.raw import RawStore

EXIT_OK, EXIT_FAILED, EXIT_PAUSED = 0, 1, 3
STREAK_TARGET = 60  # Phase 1 exit criterion
LAST_COMPLETE = "last_complete_trade_date"


@dataclass(frozen=True)
class Paths:
    root: Path

    @property
    def config_dir(self) -> Path:
        return self.root / "config"

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "pi.duckdb"

    @property
    def snapshot_path(self) -> Path:
        return self.data_dir / "pi_read.duckdb"

    @property
    def lock_path(self) -> Path:
        return self.data_dir / ".run.lock"

    def ensure_dirs(self) -> None:
        for sub in ("inbox", "processed", "quarantine", "raw"):
            (self.data_dir / sub).mkdir(parents=True, exist_ok=True)


@dataclass
class RunResult:
    status: str  # ok | noop | no_data | paused_missing | failed
    processed: list[date] = field(default_factory=list)
    missing: list[date] = field(default_factory=list)
    streak: int = 0
    message: str = ""

    @property
    def exit_code(self) -> int:
        if self.status in ("ok", "noop"):
            return EXIT_OK
        if self.status == "failed":
            return EXIT_FAILED
        return EXIT_PAUSED


class RunLocked(Exception):
    pass


@contextmanager
def run_lock(path: Path) -> Iterator[None]:
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RunLocked("another `pi run` is in progress") from exc
        yield
    finally:
        os.close(fd)  # closing the descriptor releases the lock


def missing_day_message(d: date) -> str:
    return (
        f"No bhavcopy for {d.isoformat()}. Drop that day's NSE files into data/inbox/, or, "
        f"if {d.isoformat()} was an exchange holiday, add it to config/calendar.yaml."
    )


def _log(
    con: duckdb.DuckDBPyConnection,
    run_id: str,
    d: date | None,
    status: str,
    now: datetime,
    detail: object = None,
) -> None:
    con.execute(
        "INSERT INTO run_log VALUES (?, ?, ?, ?, ?, ?)",
        [run_id, d, now, now, status, None if detail is None else json.dumps(detail, default=str)],
    )


def _process_day(
    con: duckdb.DuckDBPyConnection, store: RawStore, d: date, run_id: str, now: datetime
) -> None:
    con.begin()
    try:
        rows = store.as_of("bhavcopy", decision_cutoff(d), start=d, end=d)
        if rows.height == 0:
            raise RuntimeError(f"bhavcopy for {d} exists but is not known by {d}'s cutoff")
        report = securities.sync_day(con, d, rows, now)
        _log(
            con,
            run_id,
            d,
            "ok",
            now,
            {
                "refdata": {
                    "skipped": report.skipped,
                    "new": len(report.new_securities),
                    "provisional": report.provisional,
                    "symbol_changes": report.symbol_changes,
                    "series_changes": report.series_changes,
                }
            },
        )
        last = db.get_state(con, LAST_COMPLETE)
        if last is None or d > date.fromisoformat(last):
            db.set_state(con, LAST_COMPLETE, d.isoformat(), now)
        con.commit()
    except Exception as exc:
        con.rollback()
        _log(con, run_id, d, "failed", now, {"error": repr(exc)})
        raise


def _summarise(
    con: duckdb.DuckDBPyConnection,
    store: RawStore,
    holidays: frozenset[date],
    present: set[date],
    now: datetime,
) -> int:
    last = db.get_state(con, LAST_COMPLETE)
    ok_runs = con.execute(
        "SELECT trade_date, max(finished_at) FROM run_log "
        "WHERE status = 'ok' AND trade_date IS NOT NULL GROUP BY trade_date"
    ).fetchall()
    ok_days = {r[0] for r in ok_runs}
    streak = clean_streak(date.fromisoformat(last), ok_days, holidays, present) if last else 0

    completed_at = {r[0]: require_aware(r[1]) for r in ok_runs}
    pending = sorted(
        {
            c.partition_date
            for c in store.corrections("bhavcopy")
            if c.partition_date in completed_at and c.ingested_at > completed_at[c.partition_date]
        }
    )
    db.set_state(con, "clean_streak", str(streak), now)
    db.set_state(con, "corrections_pending", json.dumps([d.isoformat() for d in pending]), now)
    return streak


def run(root: Path, now: datetime | None = None, force_date: date | None = None) -> RunResult:
    """Run the evening pipeline for every pending trade date (or just `force_date`)."""
    now = require_aware(now or utc_now())
    paths = Paths(root)
    cfg = load_all(paths.config_dir)  # fail before touching data if config is invalid
    holidays = cfg.calendar.holiday_dates
    paths.ensure_dirs()
    store = RawStore(paths.raw_dir)

    with run_lock(paths.lock_path):
        con = db.connect(paths.db_path)
        try:
            db.ensure_schema(con, db.CORE_DDL)
            db.ensure_schema(con, securities.DDL)
            run_id = uuid4().hex
            present = set(store.partitions("bhavcopy"))
            try:
                result = _pending(con, store, holidays, present, now, run_id, force_date)
            except Exception as exc:  # recorded, then reported as a failed run
                result = RunResult("failed", message=repr(exc))
            if result.status in ("noop", "no_data"):
                _log(con, run_id, None, result.status, now, {"message": result.message})
            result.streak = _summarise(con, store, holidays, present, now)
            db.set_state(con, "last_run_status", result.status, now)
            db.set_state(con, "last_run_at", now.isoformat(), now)
            con.execute("CHECKPOINT")
        finally:
            con.close()
        db.export_snapshot(paths.db_path, paths.snapshot_path)
    return result


def _pending(
    con: duckdb.DuckDBPyConnection,
    store: RawStore,
    holidays: frozenset[date],
    present: set[date],
    now: datetime,
    run_id: str,
    force_date: date | None,
) -> RunResult:
    due = latest_due_trade_date(now)

    if force_date is not None:
        if force_date not in present:
            _log(con, run_id, force_date, "paused_missing", now, missing_day_message(force_date))
            return RunResult(
                "paused_missing", missing=[force_date], message=missing_day_message(force_date)
            )
        _process_day(con, store, force_date, run_id, now)
        return RunResult("ok", processed=[force_date])

    last = db.get_state(con, LAST_COMPLETE)
    if last is not None:
        start = date.fromordinal(date.fromisoformat(last).toordinal() + 1)
    elif present:
        start = min(present)
    else:
        return RunResult("no_data", message="No bhavcopy has been ingested yet.")
    if start > due:
        return RunResult("noop", message=f"Nothing pending; complete through {last}.")

    processed: list[date] = []
    for d in trading_days(start, due, holidays, present):
        if d not in present:
            msg = missing_day_message(d)
            _log(con, run_id, d, "paused_missing", now, msg)
            return RunResult(
                "paused_missing",
                processed=processed,
                missing=missing_days(d, due, holidays, present),
                message=msg,
            )
        _process_day(con, store, d, run_id, now)
        processed.append(d)
    if not processed:
        return RunResult("noop", message="Nothing pending.")
    return RunResult("ok", processed=processed)
