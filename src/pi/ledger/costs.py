"""Trading cost engine (WP 1.9, architecture §11.1).

Computes the charges on one executed delivery order from the dated schedule in
config/costs.yaml, plus the depository (DP) charge, which is billed once per stock per
sell day and does **not** appear on the contract note.

Amounts are unrounded rupees. The exchange's and broker's rounding rules (for example STT
rounded per contract note) are pinned in WP 1.7 against your real Groww contract notes;
until then the contract-note match tolerance of Rs 1 absorbs them. Not included yet: the
NSE investor-protection fund charge (about Rs 10 per crore, under 1 paisa on Rs 5,000).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from typing import Literal

from pi.config import CostSchedule, CostsConfig

Side = Literal["buy", "sell"]
Instrument = Literal["equity", "etf"]


@dataclass(frozen=True)
class OrderCharges:
    """Charges on one executed order, as they appear on the contract note."""

    brokerage: float
    stt: float
    exchange_txn: float
    sebi_fee: float
    stamp_duty: float
    gst: float

    @property
    def total(self) -> float:
        return (
            self.brokerage
            + self.stt
            + self.exchange_txn
            + self.sebi_fee
            + self.stamp_duty
            + self.gst
        )

    def as_dict(self) -> dict[str, float]:
        return {**asdict(self), "total": self.total}


def brokerage(value: float, s: CostSchedule) -> float:
    """Groww delivery: Rs 20 or 0.1% per executed order, whichever is lower, minimum Rs 5."""
    return max(s.brokerage_min_inr, min(s.brokerage_max_inr, s.brokerage_rate * value))


def order_charges(
    side: Side, value: float, s: CostSchedule, instrument: Instrument = "equity"
) -> OrderCharges:
    """Charges for one executed delivery order of `value` rupees."""
    if value <= 0:
        raise ValueError(f"order value must be positive, got {value}")
    if side not in ("buy", "sell"):
        raise ValueError(f"side must be buy or sell, got {side!r}")
    if instrument == "equity":
        stt_rate = s.stt_delivery_rate
    elif instrument == "etf":
        stt_rate = s.stt_etf_buy_rate if side == "buy" else s.stt_etf_sell_rate
    else:
        raise ValueError(f"instrument must be equity or etf, got {instrument!r}")

    brok = brokerage(value, s)
    exch = s.exchange_txn_rate * value
    sebi = s.sebi_fee_rate * value
    return OrderCharges(
        brokerage=brok,
        stt=stt_rate * value,
        exchange_txn=exch,
        sebi_fee=sebi,
        stamp_duty=s.stamp_duty_buy_rate * value if side == "buy" else 0.0,
        gst=s.gst_rate * (brok + exch + sebi),
    )


def dp_charge(sell_value_for_stock_day: float, s: CostSchedule) -> float:
    """DP charge, GST included, for one stock sold on one day (all its orders combined).

    Waived when that day's debit for the stock is below the schedule's threshold."""
    if sell_value_for_stock_day < s.dp_waived_below_inr:
        return 0.0
    return s.dp_charge_inr * (1 + s.gst_rate)


@dataclass(frozen=True)
class SwitchCost:
    """Cost of selling a stock and putting the proceeds into the ETF (§11.1 hurdle,
    excluding tax, which the tax engine adds)."""

    sell: OrderCharges
    dp: float
    buy_etf: OrderCharges

    @property
    def total(self) -> float:
        return self.sell.total + self.dp + self.buy_etf.total


def switch_to_etf_cost(sell_value: float, costs: CostsConfig, on: date) -> SwitchCost:
    """Selling `sell_value` of one stock on `on` and buying the ETF with the proceeds."""
    s = costs.on(on)
    sell = order_charges("sell", sell_value, s, "equity")
    dp = dp_charge(sell_value, s)
    proceeds = sell_value - sell.total - dp
    if proceeds <= 0:
        raise ValueError(f"charges exceed the sale value of Rs {sell_value:.2f}")
    return SwitchCost(sell=sell, dp=dp, buy_etf=order_charges("buy", proceeds, s, "etf"))
