"""Internal security IDs (WP 1.1, ADR-0011).

Rules:
- A `security_id` is assigned once and never deleted or reused.
- ISINs change on splits, consolidations and demergers, so an ISIN is not an identity.
  `isin_map` points every ISIN ever seen at the security it belonged to when first seen.
- A new ISIN arriving under a symbol that already belongs to another security is not
  guessed: it gets a *provisional* ID and an open issue, until a corporate-action record
  (WP 1.6) or you call `link_isin`. Linking marks the provisional ID `merged_into` the
  surviving one; every lookup follows that alias, so IDs already stored elsewhere still
  resolve.
- Symbol and series history are half-open ranges [valid_from, valid_to). A move from EQ
  to BE/BZ is a series change, not a new security.
- Only equity-like series get IDs (`ID_SERIES`); bonds, gold bonds and other rows in the
  bhavcopy are ignored here. If one ISIN appears in two of these series on a day, the
  higher-priority series wins.
- Days must be applied in order. Re-applying a day already applied is a no-op.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

import duckdb
import polars as pl

from pi.store import db
from pi.store.clock import require_aware, utc_now

# Priority order: EQ (normal), BE/BZ (trade-for-trade, surveillance), SM/ST (SME).
ID_SERIES: tuple[str, ...] = ("EQ", "BE", "BZ", "SM", "ST")
STATE_KEY = "refdata_through"

DDL = (
    """CREATE TABLE IF NOT EXISTS securities (
        security_id INTEGER PRIMARY KEY,
        first_seen DATE NOT NULL,
        provisional BOOLEAN NOT NULL,
        merged_into INTEGER
    )""",
    """CREATE TABLE IF NOT EXISTS isin_map (
        isin VARCHAR PRIMARY KEY,
        security_id INTEGER NOT NULL,
        valid_from DATE NOT NULL,
        reason VARCHAR NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS isin_links (
        new_isin VARCHAR PRIMARY KEY,
        old_isin VARCHAR NOT NULL,
        effective_date DATE NOT NULL,
        reason VARCHAR NOT NULL,
        recorded_at TIMESTAMPTZ NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS symbol_history (
        security_id INTEGER NOT NULL,
        symbol VARCHAR NOT NULL,
        valid_from DATE NOT NULL,
        valid_to DATE
    )""",
    """CREATE TABLE IF NOT EXISTS series_history (
        security_id INTEGER NOT NULL,
        series VARCHAR NOT NULL,
        valid_from DATE NOT NULL,
        valid_to DATE
    )""",
    """CREATE TABLE IF NOT EXISTS refdata_issues (
        kind VARCHAR NOT NULL,
        security_id INTEGER NOT NULL,
        isin VARCHAR NOT NULL,
        symbol VARCHAR NOT NULL,
        trade_date DATE NOT NULL,
        detail VARCHAR NOT NULL,
        opened_at TIMESTAMPTZ NOT NULL,
        resolved_at TIMESTAMPTZ,
        resolution VARCHAR
    )""",
)


class RefdataError(Exception):
    pass


@dataclass
class SyncReport:
    trade_date: date
    skipped: bool = False
    new_securities: list[int] = field(default_factory=list)
    provisional: list[int] = field(default_factory=list)
    symbol_changes: int = 0
    series_changes: int = 0


# --------------------------------------------------------------------------- lookups


def canonical(con: duckdb.DuckDBPyConnection, security_id: int) -> int:
    """Follow merged_into aliases to the surviving ID."""
    seen = set()
    sid = security_id
    while True:
        row = con.execute(
            "SELECT merged_into FROM securities WHERE security_id = ?", [sid]
        ).fetchone()
        if row is None:
            raise RefdataError(f"unknown security_id {security_id}")
        if row[0] is None:
            return sid
        if sid in seen:
            raise RefdataError(f"alias cycle at security_id {sid}")
        seen.add(sid)
        sid = int(row[0])


def security_for_isin(con: duckdb.DuckDBPyConnection, isin: str) -> int | None:
    row = con.execute("SELECT security_id FROM isin_map WHERE isin = ?", [isin]).fetchone()
    return None if row is None else canonical(con, int(row[0]))


def _value_on(con: duckdb.DuckDBPyConnection, table: str, col: str, sid: int, d: date):
    row = con.execute(
        f"SELECT {col} FROM {table} WHERE security_id = ? AND valid_from <= ? "
        "AND (valid_to IS NULL OR ? < valid_to) ORDER BY valid_from DESC LIMIT 1",
        [canonical(con, sid), d, d],
    ).fetchone()
    return None if row is None else row[0]


def symbol_on(con: duckdb.DuckDBPyConnection, security_id: int, d: date) -> str | None:
    return _value_on(con, "symbol_history", "symbol", security_id, d)


def series_on(con: duckdb.DuckDBPyConnection, security_id: int, d: date) -> str | None:
    return _value_on(con, "series_history", "series", security_id, d)


def open_issues(con: duckdb.DuckDBPyConnection) -> list[tuple]:
    return con.execute(
        "SELECT kind, security_id, isin, symbol, trade_date, detail FROM refdata_issues "
        "WHERE resolved_at IS NULL ORDER BY trade_date, security_id"
    ).fetchall()


# --------------------------------------------------------------------------- daily sync


def _primary_rows(rows: pl.DataFrame) -> pl.DataFrame:
    prio = {s: i for i, s in enumerate(ID_SERIES)}
    return (
        rows.select("isin", "symbol", "series")
        .filter(pl.col("series").is_in(list(ID_SERIES)))
        .with_columns(pl.col("series").replace_strict(prio, return_dtype=pl.Int32).alias("_p"))
        .sort("isin", "_p")
        .unique(subset="isin", keep="first", maintain_order=True)
        .drop("_p")
    )


def sync_day(
    con: duckdb.DuckDBPyConnection,
    d: date,
    rows: pl.DataFrame,
    now: datetime | None = None,
) -> SyncReport:
    """Assign IDs and update symbol/series history from one day's bhavcopy rows."""
    now = require_aware(now or utc_now())
    report = SyncReport(trade_date=d)
    through = db.get_state(con, STATE_KEY)
    if through is not None and d <= date.fromisoformat(through):
        report.skipped = True
        return report

    isin_to_sid = {
        i: int(s) for i, s in con.execute("SELECT isin, security_id FROM isin_map").fetchall()
    }
    merged = {
        int(s): int(m)
        for s, m in con.execute(
            "SELECT security_id, merged_into FROM securities WHERE merged_into IS NOT NULL"
        ).fetchall()
    }
    links = {
        n: (o, r)
        for n, o, r in con.execute("SELECT new_isin, old_isin, reason FROM isin_links").fetchall()
    }
    open_symbol = {
        int(s): v
        for s, v in con.execute(
            "SELECT security_id, symbol FROM symbol_history WHERE valid_to IS NULL"
        ).fetchall()
    }
    open_series = {
        int(s): v
        for s, v in con.execute(
            "SELECT security_id, series FROM series_history WHERE valid_to IS NULL"
        ).fetchall()
    }
    next_id = (
        int(con.execute("SELECT coalesce(max(security_id), 0) FROM securities").fetchone()[0]) + 1
    )

    def canon(sid: int) -> int:
        while sid in merged:
            sid = merged[sid]
        return sid

    for isin, symbol, series in _primary_rows(rows).iter_rows():
        sid = isin_to_sid.get(isin)
        if sid is None:
            link = links.get(isin)
            if link is not None and link[0] in isin_to_sid:
                sid = canon(isin_to_sid[link[0]])
                con.execute(
                    "INSERT INTO isin_map VALUES (?, ?, ?, ?)", [isin, sid, d, f"link:{link[1]}"]
                )
            else:
                owners = {canon(s) for s, sym in open_symbol.items() if sym == symbol}
                sid, next_id = next_id, next_id + 1
                provisional = bool(owners)
                con.execute("INSERT INTO securities VALUES (?, ?, ?, NULL)", [sid, d, provisional])
                con.execute(
                    "INSERT INTO isin_map VALUES (?, ?, ?, ?)",
                    [isin, sid, d, "new_provisional" if provisional else "new"],
                )
                report.new_securities.append(sid)
                if provisional:
                    report.provisional.append(sid)
                    con.execute(
                        "INSERT INTO refdata_issues VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL)",
                        [
                            "new_isin_for_existing_symbol",
                            sid,
                            isin,
                            symbol,
                            d,
                            f"symbol {symbol} already belongs to security "
                            f"{sorted(owners)}. If this is a split, consolidation or other "
                            "ISIN change, link the ISINs; otherwise confirm a new security.",
                            now,
                        ],
                    )
            isin_to_sid[isin] = sid
        sid = canon(sid)

        if open_symbol.get(sid) != symbol:
            if sid in open_symbol:
                con.execute(
                    "UPDATE symbol_history SET valid_to = ? "
                    "WHERE security_id = ? AND valid_to IS NULL",
                    [d, sid],
                )
                report.symbol_changes += 1
            con.execute("INSERT INTO symbol_history VALUES (?, ?, ?, NULL)", [sid, symbol, d])
            open_symbol[sid] = symbol
        if open_series.get(sid) != series:
            if sid in open_series:
                con.execute(
                    "UPDATE series_history SET valid_to = ? "
                    "WHERE security_id = ? AND valid_to IS NULL",
                    [d, sid],
                )
                report.series_changes += 1
            con.execute("INSERT INTO series_history VALUES (?, ?, ?, NULL)", [sid, series, d])
            open_series[sid] = series

    db.set_state(con, STATE_KEY, d.isoformat(), now)
    return report


# --------------------------------------------------------------------------- linking


def _rebuild_history(con: duckdb.DuckDBPyConnection, table: str, col: str, sid: int) -> None:
    """Re-chain one security's history: sort, merge equal neighbours, set valid_to."""
    rows = con.execute(
        f"SELECT {col}, valid_from FROM {table} WHERE security_id = ? ORDER BY valid_from", [sid]
    ).fetchall()
    merged: list[list] = []
    for value, start in rows:
        if merged and merged[-1][0] == value:
            continue
        merged.append([value, start])
    con.execute(f"DELETE FROM {table} WHERE security_id = ?", [sid])
    for i, (value, start) in enumerate(merged):
        end = merged[i + 1][1] if i + 1 < len(merged) else None
        con.execute(f"INSERT INTO {table} VALUES (?, ?, ?, ?)", [sid, value, start, end])


def link_isin(
    con: duckdb.DuckDBPyConnection,
    old_isin: str,
    new_isin: str,
    effective_date: date,
    reason: str,
    now: datetime | None = None,
) -> int:
    """Record that `new_isin` continues the security of `old_isin` (split, consolidation,
    face-value change). Returns the surviving security_id."""
    now = require_aware(now or utc_now())
    survivor = security_for_isin(con, old_isin)
    if survivor is None:
        raise RefdataError(f"cannot link: old ISIN {old_isin} has never been seen")
    existing = con.execute(
        "SELECT old_isin FROM isin_links WHERE new_isin = ?", [new_isin]
    ).fetchone()
    if existing is not None:
        if existing[0] != old_isin:
            raise RefdataError(f"{new_isin} is already linked to {existing[0]}")
        return survivor
    con.execute(
        "INSERT INTO isin_links VALUES (?, ?, ?, ?, ?)",
        [new_isin, old_isin, effective_date, reason, now],
    )
    current = security_for_isin(con, new_isin)
    if current is not None and current != survivor:
        con.execute(
            "UPDATE securities SET merged_into = ? WHERE security_id = ?", [survivor, current]
        )
        for table, col in (("symbol_history", "symbol"), ("series_history", "series")):
            con.execute(
                f"UPDATE {table} SET security_id = ? WHERE security_id = ?", [survivor, current]
            )
            _rebuild_history(con, table, col, survivor)
        con.execute(
            "UPDATE refdata_issues SET resolved_at = ?, resolution = ? "
            "WHERE security_id = ? AND resolved_at IS NULL",
            [now, f"linked to {old_isin} ({reason})", current],
        )
    return survivor
