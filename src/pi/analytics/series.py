"""Per-security analytics on one price series (WP 2.2, architecture §5).

Inputs are adjusted close prices in date order, ending at the as-of date D. Each function
uses only the values it is given, so no row after D can leak in. The caller passes the
point-in-time series (store.as_of plus read-time corporate-action adjustment, WP 1.6).

Conventions: 252 trading days a year, 21 a month; simple daily returns; volatility is
annualised by sqrt(252). Chart patterns and candlesticks are deliberately absent (§5).
"""

from __future__ import annotations

import math

import numpy as np
from numpy.typing import ArrayLike, NDArray

TRADING_DAYS = 252
MONTH = 21
RISKMETRICS_LAMBDA = 0.94


class InsufficientHistory(ValueError):
    pass


def _arr(x: ArrayLike, need: int = 1, what: str = "series") -> NDArray[np.float64]:
    a = np.asarray(x, dtype=np.float64)
    if a.ndim != 1:
        raise ValueError(f"{what} must be one-dimensional")
    if not np.all(np.isfinite(a)):
        raise ValueError(f"{what} contains NaN or inf")
    if a.size < need:
        raise InsufficientHistory(f"{what}: need {need} values, have {a.size}")
    return a


def returns(prices: ArrayLike) -> NDArray[np.float64]:
    p = _arr(prices, 2, "prices")
    if np.any(p <= 0):
        raise ValueError("prices must be positive")
    return p[1:] / p[:-1] - 1.0


def realised_vol(rets: ArrayLike, window: int = MONTH) -> float:
    """Annualised standard deviation of the last `window` daily returns."""
    r = _arr(rets, window, "returns")[-window:]
    return float(np.std(r, ddof=1) * math.sqrt(TRADING_DAYS))


def ewma_vol_series(rets: ArrayLike, lam: float = RISKMETRICS_LAMBDA) -> NDArray[np.float64]:
    """RiskMetrics EWMA volatility as of the close of each day (including that day's
    return), annualised. Seeded with the first return squared; treat the first ~30 values
    as warm-up."""
    r = _arr(rets, 2, "returns")
    var = np.empty_like(r)
    var[0] = r[0] ** 2
    for i in range(1, r.size):
        var[i] = lam * var[i - 1] + (1 - lam) * r[i] ** 2
    return np.sqrt(var * TRADING_DAYS)


def vol_spike_ratio(rets: ArrayLike, lookback: int = TRADING_DAYS) -> float:
    """Latest EWMA vol divided by the median EWMA vol over the past `lookback` days
    (the §11.2 'risk shift' trigger compares this with rules.yaml vol_spike_ratio)."""
    v = ewma_vol_series(_arr(rets, lookback, "returns"))[-lookback:]
    med = float(np.median(v))
    return float(v[-1] / med) if med > 0 else math.inf


def beta(asset_rets: ArrayLike, bench_rets: ArrayLike, window: int = TRADING_DAYS) -> float:
    """OLS beta of the asset on the benchmark over the last `window` aligned returns."""
    a = _arr(asset_rets, window, "asset returns")[-window:]
    b = _arr(bench_rets, window, "benchmark returns")[-window:]
    var = float(np.var(b, ddof=1))
    if var == 0:
        raise ValueError("benchmark returns have zero variance")
    return float(np.cov(a, b, ddof=1)[0, 1] / var)


def drawdown(prices: ArrayLike) -> float:
    """Current fall from the running peak of `prices` (0 at a new high, -0.2 = 20% down).
    Pass prices since purchase for the §11.2 'down from the peak since purchase' rule."""
    p = _arr(prices, 1, "prices")
    return float(p[-1] / np.max(p) - 1.0)


def max_drawdown(prices: ArrayLike) -> float:
    p = _arr(prices, 1, "prices")
    return float(np.min(p / np.maximum.accumulate(p) - 1.0))


def distance_from_high(prices: ArrayLike, window: int = TRADING_DAYS) -> float:
    """Price relative to its highest close over the last `window` days (52-week high)."""
    p = _arr(prices, window, "prices")[-window:]
    return float(p[-1] / np.max(p) - 1.0)


def momentum_12_1(prices: ArrayLike) -> float:
    """Return from 12 months ago to 1 month ago, skipping the latest month (the standard
    momentum definition, which avoids the one-month reversal effect)."""
    p = _arr(prices, TRADING_DAYS + 1, "prices")
    return float(p[-1 - MONTH] / p[-1 - TRADING_DAYS] - 1.0)


def trend(prices: ArrayLike, window: int = 200) -> float:
    """Continuous trend: price relative to its `window`-day simple moving average."""
    p = _arr(prices, window, "prices")[-window:]
    return float(p[-1] / np.mean(p) - 1.0)
