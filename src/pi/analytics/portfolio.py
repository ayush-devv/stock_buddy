"""Portfolio analytics (WP 2.2, architecture §5, requirement F4).

- Covariance uses Ledoit-Wolf shrinkage (scikit-learn): with 4-6 stocks and a year of
  daily data the raw sample covariance is noisy, and shrinkage is the standard fix.
- Risk contribution of holding i: w_i * (Sigma w)_i / sigma_p. The shares sum to 1, so
  "this stock is 40% of the portfolio's risk" is literal.
- Scenario replay applies each stock's return over a past crisis window to today's
  weights. A stock without history for that window falls back to beta x index return,
  and is reported as such. It shows exposure, not timing (§1).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

import numpy as np
from numpy.typing import ArrayLike, NDArray
from sklearn.covariance import LedoitWolf

from pi.analytics.series import TRADING_DAYS, InsufficientHistory


def weights(values: Mapping[int, float]) -> dict[int, float]:
    """Holding values (rupees) -> weights summing to 1."""
    total = sum(values.values())
    if total <= 0 or any(v < 0 for v in values.values()):
        raise ValueError("holding values must be non-negative with a positive total")
    return {k: v / total for k, v in values.items()}


def concentration(w: Mapping[int, float]) -> dict[str, float]:
    """Largest weight, Herfindahl index and its 'effective number of stocks'."""
    hhi = sum(x * x for x in w.values())
    return {"top_weight": max(w.values()), "hhi": hhi, "effective_n": 1.0 / hhi}


def shrunk_covariance(rets: ArrayLike) -> tuple[NDArray[np.float64], float]:
    """Daily-return matrix (days x stocks) -> Ledoit-Wolf covariance and shrinkage used."""
    x = np.asarray(rets, dtype=np.float64)
    if x.ndim != 2 or x.shape[0] < 30:
        raise InsufficientHistory("need a (days x stocks) matrix with at least 30 days")
    if not np.all(np.isfinite(x)):
        raise ValueError("returns contain NaN or inf; align the series first")
    lw = LedoitWolf().fit(x)
    return lw.covariance_, float(lw.shrinkage_)


@dataclass(frozen=True)
class RiskReport:
    vol: float  # annualised portfolio volatility
    contribution: dict[int, float]  # share of portfolio risk, sums to 1
    shrinkage: float


def risk_contributions(ids: list[int], w: Mapping[int, float], rets: ArrayLike) -> RiskReport:
    """`rets` columns are in the order of `ids`."""
    cov, shrink = shrunk_covariance(rets)
    wv = np.array([w[i] for i in ids], dtype=np.float64)
    var = float(wv @ cov @ wv)
    if var <= 0:
        raise ValueError("portfolio variance is zero")
    marginal = cov @ wv
    share = wv * marginal / var
    return RiskReport(
        vol=math.sqrt(var * TRADING_DAYS),
        contribution={i: float(s) for i, s in zip(ids, share, strict=True)},
        shrinkage=shrink,
    )


def active_sector_weights(
    w: Mapping[int, float], sector_of: Mapping[int, str], bench: Mapping[str, float]
) -> dict[str, float]:
    """Portfolio sector weight minus benchmark (NIFTY 500) sector weight, per sector."""
    port: dict[str, float] = {}
    for sec, x in w.items():
        port[sector_of[sec]] = port.get(sector_of[sec], 0.0) + x
    return {s: port.get(s, 0.0) - bench.get(s, 0.0) for s in sorted(set(port) | set(bench))}


@dataclass(frozen=True)
class ScenarioResult:
    portfolio_return: float
    by_stock: dict[int, float]  # contribution to the portfolio return
    proxied: list[int]  # stocks without history in the window: beta x index used


def replay(
    w: Mapping[int, float],
    stock_return: Mapping[int, float | None],
    index_return: float,
    betas: Mapping[int, float],
) -> ScenarioResult:
    """Today's weights through a past window. `stock_return[i]` is the stock's total return
    over the window, or None if it wasn't listed then."""
    by_stock, proxied = {}, []
    for sec, x in w.items():
        r = stock_return.get(sec)
        if r is None:
            r = betas[sec] * index_return
            proxied.append(sec)
        by_stock[sec] = x * r
    return ScenarioResult(sum(by_stock.values()), by_stock, sorted(proxied))
