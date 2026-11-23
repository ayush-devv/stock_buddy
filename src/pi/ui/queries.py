"""Read-only queries behind the UI (WP 2.6, ADR-0017).

Everything here takes a connection opened read-only on pi_read.duckdb, the snapshot the
evening run exports. The UI never opens pi.duckdb, so it can stay open while the run writes.
Tables a later work package creates may not exist yet; those queries return empty results.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import duckdb

from pi.store import db

STREAK_TARGET = 60  # Phase 1 exit criterion (kept in step with pi.pipeline)


def open_snapshot(path: Path) -> duckdb.DuckDBPyConnection | None:
    """Read-only connection to the snapshot, or None if no run has produced one yet."""
    if not path.exists():
        return None
    return db.connect(path, read_only=True)


def _has_table(con: duckdb.DuckDBPyConnection, name: str) -> bool:
    row = con.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name = ?", [name]
    ).fetchone()
    return bool(row and row[0])


@dataclass(frozen=True)
class Status:
    last_run_status: str | None
    last_run_at: str | None
    complete_through: str | None
    streak: int
    corrections_pending: list[str]
    open_refdata_issues: int
    gate_state: str


def status(con: duckdb.DuckDBPyConnection) -> Status:
    state = dict(con.execute("SELECT key, value FROM system_state").fetchall())
    issues = 0
    if _has_table(con, "refdata_issues"):
        issues = con.execute(
            "SELECT count(*) FROM refdata_issues WHERE resolved_at IS NULL"
        ).fetchone()[0]
    return Status(
        last_run_status=state.get("last_run_status"),
        last_run_at=state.get("last_run_at"),
        complete_through=state.get("last_complete_trade_date"),
        streak=int(state.get("clean_streak") or 0),
        corrections_pending=json.loads(state.get("corrections_pending") or "[]"),
        open_refdata_issues=int(issues),
        gate_state=state.get("gate_state", "NOT_EVALUATED"),
    )


def recent_runs(con: duckdb.DuckDBPyConnection, limit: int = 20) -> list[dict]:
    rows = con.execute(
        "SELECT trade_date, status, finished_at, detail FROM run_log "
        "ORDER BY finished_at DESC, trade_date DESC NULLS LAST LIMIT ?",
        [limit],
    ).fetchall()
    return [{"trade_date": r[0], "status": r[1], "finished_at": r[2], "detail": r[3]} for r in rows]


def alerts(con: duckdb.DuckDBPyConnection, mode: str | None = None, limit: int = 100) -> list[dict]:
    """Alerts newest first, with the local-only details. `mode`: live, shadow or None."""
    if not _has_table(con, "alerts"):
        return []
    sql = (
        "SELECT fired_at, severity, rule_id, subject, mode, delivered, error, payload_json "
        "FROM alerts"
    )
    params: list = []
    if mode is not None:
        sql += " WHERE mode = ?"
        params.append(mode)
    sql += " ORDER BY fired_at DESC LIMIT ?"
    params.append(limit)
    return [
        {
            "fired_at": r[0],
            "severity": r[1],
            "rule": r[2],
            "subject": r[3],
            "mode": r[4],
            "delivered": r[5],
            "error": r[6],
            "details": json.loads(r[7]) if r[7] else {},
        }
        for r in con.execute(sql, params).fetchall()
    ]


def shadow_counts(con: duckdb.DuckDBPyConnection) -> dict[str, int]:
    """How often each rule fired in shadow mode: the input for calibrating thresholds."""
    if not _has_table(con, "alerts"):
        return {}
    rows = con.execute(
        "SELECT rule_id, count(*) FROM alerts WHERE mode = 'shadow' GROUP BY rule_id ORDER BY 1"
    ).fetchall()
    return {r[0]: int(r[1]) for r in rows}


def refdata_issues(con: duckdb.DuckDBPyConnection) -> list[dict]:
    if not _has_table(con, "refdata_issues"):
        return []
    rows = con.execute(
        "SELECT trade_date, kind, isin, symbol, detail FROM refdata_issues "
        "WHERE resolved_at IS NULL ORDER BY trade_date"
    ).fetchall()
    return [
        {"trade_date": r[0], "kind": r[1], "isin": r[2], "symbol": r[3], "detail": r[4]}
        for r in rows
    ]
