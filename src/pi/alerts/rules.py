"""Alert rules (WP 2.4, architecture §11.2) as pure functions.

Each rule returns a `RuleResult`: the conditions true now, and the **scopes it actually
evaluated**. A scope is what a condition is about (one holding, one tax year). The engine
re-arms an active alert only when its scope was evaluated and found false, so a holding
skipped for missing data never looks like "the condition cleared" and never re-fires.

No rule predicts direction (§11.2 invariant): they describe state, risk or the calendar.

Implemented: DRAWDOWN, SURVEILLANCE, RISK_SHIFT, LTCG_SOON, LTCG_HEADROOM, CA_AHEAD.
Need data that doesn't exist yet: CRITICAL_FILING, PLEDGE, RESULTS_AHEAD (WP 2.3/2.5),
MODEL_SHIFT, MODEL_HEALTH (Phase 4).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date

from pi.analytics.series import TRADING_DAYS, drawdown, returns, vol_spike_ratio
from pi.config import AlertRules, TaxConfig
from pi.ledger.book import Lot
from pi.ledger.tax import long_term_from

# Market rules only; run alerts (DATA_MISSING and friends) live in run_alerts.
SEVERITY = {
    "DRAWDOWN": "red",
    "SURVEILLANCE": "red",
    "RISK_SHIFT": "yellow",
    "LTCG_SOON": "yellow",
    "LTCG_HEADROOM": "yellow",
    "CA_AHEAD": "yellow",
}


@dataclass(frozen=True)
class Condition:
    rule_id: str
    scope: str  # what it is about, e.g. "sec:12"
    key: str  # unique within the rule; may be more specific than scope
    subject: str | None  # ticker or ISO date: the only thing Telegram may show
    security_id: int | None = None
    payload: dict = field(default_factory=dict)  # details for the local UI only

    @property
    def severity(self) -> str:
        return SEVERITY[self.rule_id]


@dataclass(frozen=True)
class RuleResult:
    rule_id: str
    evaluated: frozenset[str]  # scopes actually checked today
    conditions: tuple[Condition, ...]

    def __post_init__(self) -> None:
        stray = {c.scope for c in self.conditions} - self.evaluated
        if stray or any(c.rule_id != self.rule_id for c in self.conditions):
            raise ValueError(f"{self.rule_id}: conditions outside the evaluated scopes {stray}")


def _result(rule_id: str, evaluated: set[str], conditions: list[Condition]) -> RuleResult:
    return RuleResult(rule_id, frozenset(evaluated), tuple(conditions))


@dataclass(frozen=True)
class Holding:
    security_id: int
    ticker: str
    prices_since_purchase: Sequence[float]  # adjusted closes from the first buy to today
    index_since_purchase: Sequence[float]  # NIFTY 500 TRI over the same days
    stages: tuple[str, ...] = ()  # current surveillance stages
    history: Sequence[float] = ()  # >= 1 year of adjusted closes, regardless of purchase


def drawdown_rule(holdings: Sequence[Holding], cfg: AlertRules) -> RuleResult:
    """🔴 Down >= drawdown_from_peak_pct from the peak since purchase AND trailing the
    index by >= drawdown_vs_index_pct since purchase (both, so a market-wide fall alone
    doesn't fire it). Holdings with misaligned or too-short data are not evaluated."""
    evaluated, out = set(), []
    for h in holdings:
        p, ix = h.prices_since_purchase, h.index_since_purchase
        if len(p) < 2 or len(ix) != len(p):
            continue
        scope = f"sec:{h.security_id}"
        evaluated.add(scope)
        dd = drawdown(p)
        rel = (p[-1] / p[0]) - (ix[-1] / ix[0])
        if dd <= -cfg.drawdown_from_peak_pct / 100 and rel <= -cfg.drawdown_vs_index_pct / 100:
            out.append(
                Condition(
                    "DRAWDOWN",
                    scope,
                    scope,
                    h.ticker,
                    h.security_id,
                    {"drawdown": dd, "vs_index": rel},
                )
            )
    return _result("DRAWDOWN", evaluated, out)


def surveillance_rule(holdings: Sequence[Holding]) -> RuleResult:
    """🔴 A holding is under exchange surveillance. The key includes the stages, so a
    stage *change* is a new condition and alerts again."""
    evaluated, out = set(), []
    for h in holdings:
        scope = f"sec:{h.security_id}"
        evaluated.add(scope)
        if h.stages:
            key = f"{scope}:{'+'.join(sorted(h.stages))}"
            out.append(
                Condition(
                    "SURVEILLANCE", scope, key, h.ticker, h.security_id, {"stages": list(h.stages)}
                )
            )
    return _result("SURVEILLANCE", evaluated, out)


def risk_shift_rule(holdings: Sequence[Holding], cfg: AlertRules) -> RuleResult:
    """🟡 EWMA volatility at or above vol_spike_ratio x its 1-year median. Uses the
    holding's full `history`, so it works from the day you buy."""
    evaluated, out = set(), []
    for h in holdings:
        if len(h.history) < TRADING_DAYS + 2:
            continue
        scope = f"sec:{h.security_id}"
        evaluated.add(scope)
        ratio = vol_spike_ratio(returns(h.history))
        if ratio >= cfg.vol_spike_ratio:
            out.append(
                Condition("RISK_SHIFT", scope, scope, h.ticker, h.security_id, {"ratio": ratio})
            )
    return _result("RISK_SHIFT", evaluated, out)


def ltcg_soon_rule(
    lots: Sequence[Lot], tickers: Mapping[int, str], today: date, tax: TaxConfig, cfg: AlertRules
) -> RuleResult:
    """🟡 Shares turn long-term within ltcg_window_days: selling before then costs 20%
    STCG instead of 12.5% LTCG above the exemption. One condition per stock, demat and
    long-term date. Keys never use lot ids, which shift when older trades are imported."""
    evaluated: set[str] = set()
    due: dict[tuple[int, str, date], int] = defaultdict(int)
    for lot in lots:
        if lot.qty <= 0:
            continue
        evaluated.add(f"sec:{lot.security}:{lot.demat}")
        lt = long_term_from(lot.acquired, tax)
        if 0 < (lt - today).days <= cfg.ltcg_window_days:
            due[(lot.security, lot.demat, lt)] += lot.qty
    out = [
        Condition(
            "LTCG_SOON",
            f"sec:{sec}:{demat}",
            f"ltcg:{sec}:{demat}:{lt.isoformat()}",
            tickers.get(sec),
            sec,
            {"long_term_from": lt.isoformat(), "qty": qty},
        )
        for (sec, demat, lt), qty in sorted(due.items())
    ]
    return _result("LTCG_SOON", evaluated, out)


def ltcg_headroom_rule(used: float, limit: float, fy: int, cfg: AlertRules) -> RuleResult:
    """🟡 At least ltcg_headroom_warn_pct of this year's LTCG exemption is used."""
    scope = f"fy:{fy}"
    out = []
    if limit > 0 and used / limit * 100 >= cfg.ltcg_headroom_warn_pct:
        out.append(
            Condition("LTCG_HEADROOM", scope, scope, None, None, {"used": used, "limit": limit})
        )
    return _result("LTCG_HEADROOM", {scope}, out)


@dataclass(frozen=True)
class CorporateEvent:
    security_id: int
    ticker: str
    ex_date: date
    kind: str


def ca_ahead_rule(events: Sequence[CorporateEvent], today: date, cfg: AlertRules) -> RuleResult:
    """🟡 A corporate action on a holding goes ex within ca_exdate_days. One global scope:
    the whole event list is checked each time, so past events re-arm cleanly."""
    out = [
        Condition(
            "CA_AHEAD",
            "events",
            f"sec:{e.security_id}:{e.kind}:{e.ex_date}",
            e.ticker,
            e.security_id,
            {"kind": e.kind, "ex_date": e.ex_date.isoformat()},
        )
        for e in events
        if 0 <= (e.ex_date - today).days <= cfg.ca_exdate_days
    ]
    return _result("CA_AHEAD", {"events"}, out)
