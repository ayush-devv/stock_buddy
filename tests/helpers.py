"""Synthetic exchange data for tests. No real NSE data is used here."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import date

import polars as pl


def bhav_rows(
    d: date,
    rows: Sequence[tuple[str, str, str]] = (("INE000A01011", "AAA", "EQ"),),
    close: float = 100.0,
) -> pl.DataFrame:
    """A canonical bhavcopy frame: one row per (isin, symbol, series)."""
    n = len(rows)
    return pl.DataFrame(
        {
            "trade_date": [d] * n,
            "isin": [r[0] for r in rows],
            "symbol": [r[1] for r in rows],
            "series": [r[2] for r in rows],
            "open": [close] * n,
            "high": [close] * n,
            "low": [close] * n,
            "close": [close] * n,
            "prev_close": [close] * n,
            "volume": [1000] * n,
            "value": [close * 1000] * n,
            "trades": [10] * n,
        }
    )


def sha_of(frame: pl.DataFrame) -> str:
    return hashlib.sha256(frame.write_csv().encode()).hexdigest()
