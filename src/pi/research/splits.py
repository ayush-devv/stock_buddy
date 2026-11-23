"""Purged, embargoed, expanding walk-forward folds (architecture §14.1, ADR-0014).

Everything is counted in **trading-day indices** (positions in the trading calendar), not
calendar days. A sample at index i has the §8 label window from the open of i+1 to the
open of i+1+horizon, so its outcome is known only on day i+1+horizon.

For a test fold whose first sample is at index t0, a training sample j is kept only if
its label was fully known, plus an embargo gap, before t0:

    j + 1 + horizon + embargo <= t0

Training samples whose labels overlap the test period are purged. The embargo is an
extra gap against serial correlation. Purge and embargo come from gate_spec.yaml, not
from callers.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Fold:
    train: tuple[int, ...]  # sample positions (indices into the samples list)
    test: tuple[int, ...]


def walk_forward(
    sample_days: Sequence[int],
    horizon: int,
    embargo: int,
    test_size: int,
    min_train: int,
) -> list[Fold]:
    """Folds over samples located at trading-day indices `sample_days` (strictly rising).

    Test folds are consecutive blocks of `test_size` samples. Training is everything
    earlier that passes the purge and embargo rule, and must hold at least `min_train`
    samples or the fold is skipped."""
    days = list(sample_days)
    if any(b <= a for a, b in zip(days, days[1:], strict=False)):
        raise ValueError("sample days must be strictly increasing trading-day indices")
    if min(horizon, test_size, min_train) < 1 or embargo < 0:
        raise ValueError("horizon, test_size and min_train must be >= 1; embargo >= 0")

    folds = []
    start = 0
    while start < len(days):
        test = list(range(start, min(start + test_size, len(days))))
        t0 = days[test[0]]
        train = [j for j in range(test[0]) if days[j] + 1 + horizon + embargo <= t0]
        if len(train) >= min_train:
            folds.append(Fold(tuple(train), tuple(test)))
        start += test_size
    return folds
