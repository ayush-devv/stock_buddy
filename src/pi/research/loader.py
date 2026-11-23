"""The only way research code reads data (architecture §14.2, WP 3.5).

It enforces the lockbox cutoff:
- no row on or after the lockbox start date is ever returned;
- data is read **as it was known** at the end of the day before the lockbox starts, so a
  correction that arrived during the lockbox period can't leak in;
- a sample date is allowed only if its whole label window (the next open, then
  `purge_trading_days` trading days) ends before the lockbox starts.

Models (pi.model) may not import pi.store; an import-linter contract enforces that, and
the source scan keeps research/ scripts off pi.store too.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, timedelta

import polars as pl

from pi.config import GateSpec
from pi.research.lockbox import Seal
from pi.research.splits import Fold, walk_forward
from pi.store.clock import decision_cutoff
from pi.store.raw import RawStore


class ResearchLoader:
    def __init__(
        self, store: RawStore, trading_days: Sequence[date], seal: Seal, spec: GateSpec
    ) -> None:
        days = list(trading_days)
        if any(b <= a for a, b in zip(days, days[1:], strict=False)):
            raise ValueError("trading days must be strictly increasing")
        self._store = store
        self._days = days
        self._cutoff = seal.start
        self._spec = spec
        self._horizon = spec.walk_forward.purge_trading_days  # >= the label horizon

    @property
    def last_data_day(self) -> date:
        return self._cutoff - timedelta(days=1)

    def frame(
        self, source: str, start: date | None = None, end: date | None = None
    ) -> pl.DataFrame:
        """Rows of `source` known by the end of the day before the lockbox, never on or
        after the lockbox start, whatever `end` is asked for."""
        last = self.last_data_day if end is None else min(end, self.last_data_day)
        return self._store.as_of(source, decision_cutoff(self.last_data_day), start, last)

    def sample_days(self) -> list[date]:
        """Trading days whose full label window ends before the lockbox starts."""
        lock_idx = next((i for i, d in enumerate(self._days) if d >= self._cutoff), len(self._days))
        return [d for i, d in enumerate(self._days) if i + 1 + self._horizon < lock_idx]

    def folds(self, samples: Sequence[date], test_size: int, min_train: int) -> list[Fold]:
        """Walk-forward folds over `samples` with purge and embargo taken from the spec."""
        allowed = set(self.sample_days())
        stray = [d for d in samples if d not in allowed]
        if stray:
            raise ValueError(f"samples whose labels would reach the lockbox: {stray[:3]}")
        index = {d: i for i, d in enumerate(self._days)}
        return walk_forward(
            [index[d] for d in samples],
            horizon=self._horizon,
            embargo=self._spec.walk_forward.embargo_trading_days,
            test_size=test_size,
            min_train=min_train,
        )
