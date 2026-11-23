"""Guard: can you legally and practically make this trade? (WP 1.13, architecture §10)

`check()` returns every rule that applies. Its verdict overrides any action from
`decide`. Outcomes, from strongest:
- block: the action must not be shown;
- needs_preclearance: shown only with "get pre-clearance under your company's code first";
- warn: shown with the reason.

Inputs are plain values. The ledger, surveillance lists and events calendar fill them in
(WP 1.8, 1.12 and 2.3). Unknown inputs fail safe: for a restricted stock, an unknown
trading-window state blocks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Literal

from pi.config import GuardRules, RestrictedEntry
from pi.ledger.tax import add_months
from pi.store.clock import require_aware

Action = Literal["BUY", "SELL"]  # ADD buys, TRIM sells
Outcome = Literal["block", "needs_preclearance", "warn"]
T2T_SERIES = ("BE", "BZ")
SME_SERIES = ("SM", "ST")
_STRENGTH = {"block": 3, "needs_preclearance": 2, "warn": 1}
_GSM = re.compile(r"^GSM-(\d+)$")


@dataclass(frozen=True)
class Finding:
    rule: str
    outcome: Outcome
    reason: str


@dataclass(frozen=True)
class TradeContext:
    security_id: int
    action: Action
    qty: int
    price: float
    at: datetime  # when the decision is made
    series: str = "EQ"
    restricted: tuple[RestrictedEntry, ...] = ()  # entries for this security
    window_closed: bool | None = None  # None = unknown (fails safe if restricted)
    last_opposite_trade: date | None = None  # last BUY when selling, last SELL when buying
    surveillance: tuple[str, ...] = ()  # current stages, e.g. ("GSM-2", "ASM-LT-1")
    at_lower_circuit: bool = False
    at_upper_circuit: bool = False
    band_cut: bool = False
    credited_qty: int | None = None  # for SELL: shares actually in your demat
    bought_today_qty: int = 0  # for SELL: same-day purchases of this stock
    lot_size: int | None = None  # SME lot size


@dataclass(frozen=True)
class GuardResult:
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def verdict(self) -> Outcome | Literal["pass"]:
        if not self.findings:
            return "pass"
        return max(self.findings, key=lambda f: _STRENGTH[f.outcome]).outcome

    @property
    def blocked(self) -> bool:
        return self.verdict == "block"


def latest_quarter_end(d: date) -> date:
    """The most recent calendar quarter end on or before d (31 Mar, 30 Jun, 30 Sep, 31 Dec)."""
    candidates = [
        date(d.year, 3, 31),
        date(d.year, 6, 30),
        date(d.year, 9, 30),
        date(d.year, 12, 31),
    ]
    past = [q for q in candidates if q <= d]
    return max(past) if past else date(d.year - 1, 12, 31)


def trading_window_closed(at: datetime, results_at: datetime | None, buffer_hours: int) -> bool:
    """SEBI PIT Schedule B: closed from the end of each quarter until `buffer_hours` (at
    least 48) after that quarter's results. `results_at` is the declaration time of the
    results for the latest quarter end on or before `at`; None means not declared yet.
    Conservatively closed on the quarter-end date itself; your company code may differ."""
    at = require_aware(at)
    if results_at is None:
        return True
    return at < require_aware(results_at) + timedelta(hours=buffer_hours)


def _insider(ctx: TradeContext) -> list[Finding]:
    out: list[Finding] = []
    for entry in ctx.restricted:
        if entry.basis == "holds_upsi":
            out.append(
                Finding(
                    "upsi",
                    "block",
                    "You hold unpublished price-sensitive "
                    "information about this stock (PIT Reg 4).",
                )
            )
            continue
        if ctx.window_closed is None:
            out.append(
                Finding(
                    "trading_window",
                    "block",
                    "Trading-window state unknown (results date missing); "
                    "blocked until it is known.",
                )
            )
        elif ctx.window_closed:
            out.append(Finding("trading_window", "block", "Trading window is closed."))
        if ctx.last_opposite_trade is not None:
            ends = add_months(ctx.last_opposite_trade, entry.contra_trade_months)
            if ctx.at.date() < ends:
                out.append(
                    Finding(
                        "contra_trade",
                        "block",
                        f"Opposite trade on {ctx.last_opposite_trade}; contra "
                        f"period runs to {ends}.",
                    )
                )
        threshold = entry.preclearance_threshold_inr
        if threshold is not None and ctx.qty * ctx.price > threshold:
            out.append(
                Finding(
                    "preclearance",
                    "needs_preclearance",
                    f"Value above Rs {threshold:,.0f}: pre-clearance required.",
                )
            )
    return out


def _surveillance(ctx: TradeContext, rules: GuardRules) -> list[Finding]:
    out: list[Finding] = []
    for stage in ctx.surveillance:
        m = _GSM.match(stage)
        level = int(m.group(1)) if m else None
        if ctx.action == "BUY" and level is not None and level >= rules.gsm_block_buy_from_stage:
            out.append(Finding("gsm_buy", "block", f"{stage}: buying is blocked by brokers."))
        elif level is not None and level >= rules.gsm_weekly_from_stage:
            out.append(
                Finding("gsm_weekly", "warn", f"{stage}: trades only in the weekly session.")
            )
        else:
            out.append(Finding("surveillance", "warn", f"Under exchange surveillance: {stage}."))
    return out


def check(ctx: TradeContext, rules: GuardRules) -> GuardResult:
    if ctx.qty <= 0 or ctx.price <= 0:
        raise ValueError("qty and price must be positive")
    findings = _insider(ctx) + _surveillance(ctx, rules)

    if ctx.series in T2T_SERIES and ctx.action == "SELL" and ctx.bought_today_qty > 0:
        findings.append(
            Finding(
                "t2t_same_day", "block", f"{ctx.series} is trade-for-trade: no same-day reversal."
            )
        )
    elif ctx.action == "SELL" and ctx.bought_today_qty > 0:
        findings.append(
            Finding(
                "same_day", "warn", "Selling shares bought today is speculative income (s.43(5))."
            )
        )

    if ctx.action == "SELL" and ctx.at_lower_circuit:
        findings.append(Finding("lower_circuit", "warn", "Locked at lower circuit: may not fill."))
    if ctx.action == "BUY" and ctx.at_upper_circuit:
        findings.append(Finding("upper_circuit", "warn", "Locked at upper circuit: may not fill."))
    if ctx.band_cut:
        findings.append(Finding("band_cut", "warn", "Price band was cut: may not fill."))

    if ctx.action == "SELL":
        if ctx.credited_qty is None:
            findings.append(
                Finding(
                    "credited_qty", "block", "Credited quantity unknown; reconcile holdings first."
                )
            )
        elif ctx.qty > ctx.credited_qty:
            findings.append(
                Finding(
                    "credited_qty",
                    "block",
                    f"Only {ctx.credited_qty} shares are credited; "
                    "the rest are pending (corporate action or settlement).",
                )
            )

    if ctx.series in SME_SERIES:
        if not ctx.lot_size:
            findings.append(Finding("sme_lot", "block", "SME lot size unknown."))
        elif ctx.qty % ctx.lot_size:
            findings.append(
                Finding("sme_lot", "block", f"SME stocks trade in lots of {ctx.lot_size}.")
            )
    return GuardResult(tuple(findings))
