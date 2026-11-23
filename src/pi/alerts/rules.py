"""Alert rules (WP 2.4, architecture §11.2) as pure functions.

Each rule looks at today's inputs and returns the conditions that are true now, as
`Condition`s. The engine turns a condition into an alert only when it *becomes* true. No
rule predicts direction (§11.2 invariant): they describe the state, risk or calendar.

Implemented here: DRAWDOWN, SURVEILLANCE, RISK_SHIFT, LTCG_SOON, LTCG_HEADROOM, CA_AHEAD.
They need filing or model data that doesn't exist yet: CRITICAL_FILING, PLEDGE,
RESULTS_AHEAD (WP 2.3/2.5), MODEL_SHIFT, MODEL_HEALTH (Phase 4).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date

from pi.analytics.series import drawdown, returns, vol_spike_ratio
from pi.config import AlertRules, TaxConfig
from pi.ledger.book import Lot
from pi.ledger.tax import long_term_from

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
    key: str  # what the condition is about, e.g. "sec:12" or "fy:2026"
    subject: str | None  # ticker or ISO date: the only thing Telegram may show
    security_id: int | None = None
    payload: dict = field(default_factory=dict)  # details for the local UI only

    @property
    def severity(self) -> str:
        return SEVERITY[self.rule_id]


@dataclass(frozen=True)
class Holding:
    security_id: int
    ticker: str
    prices_since_purchase: Sequence[float]  # adjusted closes from the first buy to today
    index_since_purchase: Sequence[float]  # NIFTY 500 TRI over the same days
    stages: tuple[str, ...] = ()  # current surveillance stages


def drawdown_rule(holdings: Sequence[Holding], cfg: AlertRules) -> list[Condition]:
    """🔴 Down >= drawdown_from_peak_pct from the peak since purchase AND trailing the
    index by >= drawdown_vs_index_pct since purchase (both, so a market-wide fall alone
    doesn't fire it)."""
    out = []
    for h in holdings:
        p, ix = h.prices_since_purchase, h.index_since_purchase
        if len(p) < 2 or len(ix) != len(p):
            continue
        dd = drawdown(p)
        rel = (p[-1] / p[0]) - (ix[-1] / ix[0])
        if dd <= -cfg.drawdown_from_peak_pct / 100 and rel <= -cfg.drawdown_vs_index_pct / 100:
            out.append(
                Condition(
                    "DRAWDOWN",
                    f"sec:{h.security_id}",
                    h.ticker,
                    h.security_id,
                    {"drawdown": dd, "vs_index": rel},
                )
            )
    return out


def surveillance_rule(holdings: Sequence[Holding]) -> list[Condition]:
    """🔴 A holding is under exchange surveillance. The key includes the stages, so a
    stage *change* is a new condition and alerts again."""
    return [
        Condition(
            "SURVEILLANCE",
            f"sec:{h.security_id}:{'+'.join(sorted(h.stages))}",
            h.ticker,
            h.security_id,
            {"stages": list(h.stages)},
        )
        for h in holdings
        if h.stages
    ]


def risk_shift_rule(holdings: Sequence[Holding], cfg: AlertRules) -> list[Condition]:
    """🟡 20-day EWMA volatility at or above vol_spike_ratio x its 1-year median."""
    out = []
    for h in holdings:
        if len(h.prices_since_purchase) < 254:
            continue  # not enough history since purchase for a 1-year median
        ratio = vol_spike_ratio(returns(h.prices_since_purchase))
        if ratio >= cfg.vol_spike_ratio:
            out.append(
                Condition(
                    "RISK_SHIFT", f"sec:{h.security_id}", h.ticker, h.security_id, {"ratio": ratio}
                )
            )
    return out


def ltcg_soon_rule(
    lots: Sequence[Lot], tickers: Mapping[int, str], today: date, tax: TaxConfig, cfg: AlertRules
) -> list[Condition]:
    """🟡 A lot turns long-term within ltcg_window_days: selling before then costs 20%
    STCG instead of 12.5% LTCG above the exemption."""
    out = []
    for lot in lots:
        lt = long_term_from(lot.acquired, tax)
        days = (lt - today).days
        if 0 < days <= cfg.ltcg_window_days and lot.qty > 0:
            out.append(
                Condition(
                    "LTCG_SOON",
                    f"lot:{lot.lot_id}",
                    tickers.get(lot.security),
                    lot.security,
                    {"long_term_from": lt.isoformat(), "qty": lot.qty},
                )
            )
    return out


def ltcg_headroom_rule(used: float, limit: float, fy: int, cfg: AlertRules) -> list[Condition]:
    """🟡 At least ltcg_headroom_warn_pct of this year's LTCG exemption is used."""
    if limit > 0 and used / limit * 100 >= cfg.ltcg_headroom_warn_pct:
        return [Condition("LTCG_HEADROOM", f"fy:{fy}", None, None, {"used": used, "limit": limit})]
    return []


@dataclass(frozen=True)
class CorporateEvent:
    security_id: int
    ticker: str
    ex_date: date
    kind: str


def ca_ahead_rule(
    events: Sequence[CorporateEvent], today: date, cfg: AlertRules
) -> list[Condition]:
    """🟡 A corporate action on a holding goes ex within ca_exdate_days."""
    return [
        Condition(
            "CA_AHEAD",
            f"sec:{e.security_id}:{e.kind}:{e.ex_date}",
            e.ticker,
            e.security_id,
            {"kind": e.kind, "ex_date": e.ex_date.isoformat()},
        )
        for e in events
        if 0 <= (e.ex_date - today).days <= cfg.ca_exdate_days
    ]
