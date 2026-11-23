"""Capital-gains tax for one financial year (WP 1.10, architecture §12). Not tax advice.

Scope: takes realized gains and losses that the ledger (WP 1.8) has already computed
(proceeds minus cost minus allowable transfer expenses; STT is never deductible) and works
out the year's tax.
- Holding period: long-term means held MORE than 12 months; same-day buy and sell is
  speculative.
- Set-off: short-term losses against STCG, then LTCG. Long-term losses only against LTCG.
  Losses go against the higher-rate bucket first. This year's losses are applied before
  brought-forward ones, and all set-off happens before the LTCG exemption.
- The LTCG exemption is one limit per financial year, looked up at 31 March.
- Rates are looked up on each sale date (they changed on 23 Jul 2024).
- Every result lists the tax.yaml rules it used and their confidence, so a number that
  rests on a `secondary` or `unverified` rule can be marked as such.

Out of scope, by design:
- income taxed at slab rates: speculative income and deemed-dividend buybacks
  (Oct 2024 to Mar 2026). The amount is returned with `needs_slab=True`; its tax is not
  computed;
- surcharge (assumption A9: income under Rs 50 lakh);
- lots bought before 1 Feb 2018, which need grandfathered cost;
- s.94(7) dividend stripping, which needs dividend records (WP 1.8);
- expiry of carried-forward losses after 8 years.
"""

from __future__ import annotations

import calendar
import contextlib
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Literal

from pi.config import ConfigError, TaxConfig

GainKind = Literal["short", "long", "speculative"]
GRANDFATHER_CUTOFF = date(2018, 2, 1)


class TaxScopeError(ValueError):
    """A case this engine deliberately does not handle."""


# --------------------------------------------------------------------------- dates


def fy_start_year(d: date) -> int:
    """Financial year label: FY 2026 runs 1 Apr 2026 to 31 Mar 2027."""
    return d.year if d.month >= 4 else d.year - 1


def fy_bounds(fy: int) -> tuple[date, date]:
    return date(fy, 4, 1), date(fy + 1, 3, 31)


def add_months(d: date, months: int) -> date:
    """Same day-of-month `months` later, clamped to the month's end (29 Feb -> 28 Feb)."""
    m = d.month - 1 + months
    year, month = d.year + m // 12, m % 12 + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def long_term_from(acquired: date, tax: TaxConfig) -> date:
    """First sale date on which a lot bought on `acquired` is long-term."""
    months = int(tax.value("ltcg_holding_months", acquired))
    return add_months(acquired, months) + timedelta(days=1)


def holding_kind(acquired: date, sold: date, tax: TaxConfig) -> GainKind:
    if acquired < GRANDFATHER_CUTOFF:
        raise TaxScopeError(f"lot bought {acquired} needs grandfathered cost (pre 1 Feb 2018)")
    if sold < acquired:
        raise ValueError(f"sold {sold} before acquired {acquired}")
    if sold == acquired:
        return "speculative"
    return "long" if sold >= long_term_from(acquired, tax) else "short"


def buyback_regime(announced: date, bought_back: date, tax: TaxConfig) -> str:
    """Shareholder-side treatment of a listed-share buyback (see tax.yaml comments)."""
    key = announced if announced <= date(2019, 7, 5) else bought_back
    return str(tax.value("buyback_regime", key))


# --------------------------------------------------------------------------- year computation


@dataclass(frozen=True)
class RealizedGain:
    sold_on: date
    kind: GainKind
    amount: float  # positive gain, negative loss, in rupees
    label: str = ""


@dataclass(frozen=True)
class CarryForward:
    short_loss: float = 0.0  # positive numbers
    long_loss: float = 0.0

    def __post_init__(self) -> None:
        if self.short_loss < 0 or self.long_loss < 0:
            raise ValueError("carried-forward losses are positive amounts")


@dataclass
class FYTax:
    fy: int
    stcg_taxable: dict[float, float]  # rate -> taxable amount after set-off
    ltcg_taxable: dict[float, float]  # rate -> taxable amount after set-off and exemption
    exemption_limit: float
    exemption_used: float
    tax_before_cess: float
    cess: float
    carry_forward_out: CarryForward
    speculative_income: float
    needs_slab: bool
    rules_used: dict[str, str] = field(default_factory=dict)  # rule id -> confidence

    @property
    def total_tax(self) -> float:
        return self.tax_before_cess + self.cess


def _absorb(buckets: dict[float, float], loss: float) -> float:
    """Set `loss` off against buckets, highest rate first. Returns the unused loss."""
    for rate in sorted(buckets, reverse=True):
        used = min(buckets[rate], loss)
        buckets[rate] -= used
        loss -= used
        if loss <= 0:
            return 0.0
    return loss


def compute_fy(
    gains: list[RealizedGain],
    tax: TaxConfig,
    fy: int,
    brought_forward: CarryForward | None = None,
) -> FYTax:
    start, end = fy_bounds(fy)
    bf = brought_forward or CarryForward()
    used: dict[str, str] = {}

    def rule(rule_id: str, on: date):
        r = tax.rule(rule_id, on)
        used[rule_id] = r.confidence
        return r.value

    st_gain: dict[float, float] = {}
    lt_gain: dict[float, float] = {}
    st_loss = lt_loss = speculative = 0.0
    for g in gains:
        if not start <= g.sold_on <= end:
            raise ValueError(f"{g.label or g.sold_on}: sold outside FY {fy}")
        if g.kind == "speculative":
            speculative += g.amount
            continue
        if g.kind == "short":
            if g.amount >= 0:
                rate = float(rule("stcg_rate", g.sold_on))
                st_gain[rate] = st_gain.get(rate, 0.0) + g.amount
            else:
                st_loss += -g.amount
        elif g.kind == "long":
            if g.amount >= 0:
                rate = float(rule("ltcg_rate", g.sold_on))
                lt_gain[rate] = lt_gain.get(rate, 0.0) + g.amount
            else:
                lt_loss += -g.amount
        else:
            raise ValueError(f"unknown gain kind {g.kind!r}")

    # This year's losses first.
    st_left = _absorb(st_gain, st_loss)
    st_left = _absorb(lt_gain, st_left)
    lt_left = _absorb(lt_gain, lt_loss)
    # Then brought-forward losses (short: against STCG then LTCG; long: LTCG only).
    bf_st_left = _absorb(lt_gain, _absorb(st_gain, bf.short_loss))
    bf_lt_left = _absorb(lt_gain, bf.long_loss)

    # Annual LTCG exemption, against the higher-rate LTCG first.
    limit = float(rule("ltcg_exemption_inr", end))
    remaining = limit
    for rate in sorted(lt_gain, reverse=True):
        used_here = min(lt_gain[rate], remaining)
        lt_gain[rate] -= used_here
        remaining -= used_here

    # Recorded for its confidence; this engine never applies a rebate against STCG (the
    # new regime denies it from FY 2025-26; earlier years have no entry in tax.yaml).
    with contextlib.suppress(ConfigError):
        rule("stcg_87a_rebate", end)
    tax_before = sum(r * a for r, a in st_gain.items()) + sum(r * a for r, a in lt_gain.items())
    cess = tax_before * float(rule("cess_rate", end))
    return FYTax(
        fy=fy,
        stcg_taxable=st_gain,
        ltcg_taxable=lt_gain,
        exemption_limit=limit,
        exemption_used=limit - remaining,
        tax_before_cess=tax_before,
        cess=cess,
        carry_forward_out=CarryForward(
            short_loss=st_left + bf_st_left, long_loss=lt_left + bf_lt_left
        ),
        speculative_income=speculative,
        needs_slab=speculative != 0,
        rules_used=used,
    )


def marginal_tax(
    existing: list[RealizedGain],
    candidate: list[RealizedGain],
    tax: TaxConfig,
    fy: int,
    brought_forward: CarryForward | None = None,
) -> float:
    """Extra tax this year from adding `candidate` sales to the year's other realized
    gains (including the ETF shadow's). Negative when the sale realizes a useful loss."""
    with_sale = compute_fy(existing + candidate, tax, fy, brought_forward)
    without = compute_fy(existing, tax, fy, brought_forward)
    return with_sale.total_tax - without.total_tax
