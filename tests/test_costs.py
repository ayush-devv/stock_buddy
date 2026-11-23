"""WP 1.9: cost engine. Expected values are worked by hand from config/costs.yaml:

brokerage min(Rs 20, 0.1%) floored at Rs 5; STT 0.1% (shares, both sides) or 0.001%
(ETF, sell only); exchange 0.00297%; SEBI Rs 10/crore; stamp 0.015% (buy only);
GST 18% on brokerage + exchange + SEBI; DP Rs 20 + GST = Rs 23.60 per stock per sell day.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from pi.config import ConfigError, load_all
from pi.ledger.costs import (
    brokerage,
    dp_charge,
    order_charges,
    switch_to_etf_cost,
)

ROOT = Path(__file__).resolve().parents[1]
COSTS = load_all(ROOT / "config").costs
ON = date(2026, 11, 23)
S = COSTS.on(ON)
approx = pytest.approx


def test_buy_10000_shares() -> None:
    c = order_charges("buy", 10_000, S)
    assert c.brokerage == approx(10.0)  # 0.1% = 10, under the Rs 20 cap
    assert c.stt == approx(10.0)
    assert c.exchange_txn == approx(0.297)
    assert c.sebi_fee == approx(0.01)
    assert c.stamp_duty == approx(1.5)
    assert c.gst == approx(0.18 * 10.307)
    assert c.total == approx(23.66226)


def test_sell_5000_shares_is_about_0_7_percent_with_dp() -> None:
    c = order_charges("sell", 5_000, S)
    assert c.stamp_duty == 0.0
    assert c.total == approx(11.08113)
    assert dp_charge(5_000, S) == approx(23.60)
    assert (c.total + dp_charge(5_000, S)) / 5_000 == approx(0.006936, abs=1e-6)


def test_sell_2500_hits_the_minimum_brokerage_and_costs_about_1_3_percent() -> None:
    c = order_charges("sell", 2_500, S)
    assert c.brokerage == approx(5.0)  # 0.1% would be 2.50
    assert c.total == approx(8.490565)
    assert (c.total + dp_charge(2_500, S)) / 2_500 == approx(0.012836, abs=1e-6)


def test_sell_50000_hits_the_brokerage_cap() -> None:
    c = order_charges("sell", 50_000, S)
    assert c.brokerage == approx(20.0)
    assert c.total == approx(75.4113)


def test_etf_pays_no_stt_on_purchase_and_0_001_percent_on_sale() -> None:
    buy = order_charges("buy", 10_000, S, "etf")
    sell = order_charges("sell", 10_000, S, "etf")
    assert buy.stt == 0.0 and buy.total == approx(13.66226)
    assert sell.stt == approx(0.1) and sell.total == approx(12.26226)


def test_dp_is_waived_below_rs_100() -> None:
    assert dp_charge(99.99, S) == 0.0
    assert dp_charge(100.0, S) == approx(23.60)


def test_switching_5000_into_the_etf() -> None:
    sw = switch_to_etf_cost(5_000, COSTS, ON)
    assert sw.sell.total == approx(11.08113)
    assert sw.dp == approx(23.60)
    # Proceeds Rs 4,965.32 buy the ETF: Rs 5 brokerage floor, stamp, exchange, GST, no STT.
    assert sw.buy_etf.brokerage == approx(5.0)
    assert sw.buy_etf.stt == 0.0
    assert sw.buy_etf.total == approx(6.82467, abs=1e-4)
    assert sw.total == approx(41.5058, abs=1e-3)


def test_the_schedule_is_chosen_by_date() -> None:
    with pytest.raises(ConfigError, match="no entry covers"):
        switch_to_etf_cost(5_000, COSTS, date(2023, 12, 31))  # before the first schedule


@pytest.mark.parametrize(
    ("on", "min_brokerage", "exchange_rate", "dp_before_gst"),
    [
        (date(2024, 9, 30), 2.0, 0.0000322, 13.5),  # NSE's old cash transaction charge
        (date(2024, 10, 1), 2.0, 0.0000297, 13.5),  # uniform NSE charge from 1 Oct 2024
        (date(2025, 6, 20), 2.0, 0.0000297, 13.5),
        (date(2025, 6, 21), 5.0, 0.0000297, 20.0),  # Groww's new minimum and DP charge
    ],
)
def test_older_trades_use_the_charges_in_force_then(
    on: date, min_brokerage: float, exchange_rate: float, dp_before_gst: float
) -> None:
    s = COSTS.on(on)
    assert (s.brokerage_min_inr, s.exchange_txn_rate, s.dp_charge_inr) == (
        min_brokerage,
        exchange_rate,
        dp_before_gst,
    )
    assert order_charges("sell", 1_000, s).brokerage == approx(min_brokerage)  # 0.1% = Rs 1


@pytest.mark.parametrize(
    ("args", "match"),
    [
        (("sell", 0.0), "positive"),
        (("hold", 100.0), "side"),
    ],
)
def test_bad_orders_are_refused(args: tuple, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        order_charges(*args, S)


def test_bad_instrument_is_refused() -> None:
    with pytest.raises(ValueError, match="instrument"):
        order_charges("buy", 100.0, S, "bond")  # type: ignore[arg-type]


def test_charges_larger_than_the_sale_are_refused() -> None:
    with pytest.raises(ValueError, match="exceed"):
        switch_to_etf_cost(5.0, COSTS, ON)  # Rs 5 brokerage floor alone eats the sale


@given(st.floats(min_value=1, max_value=5_000_000, allow_nan=False))
def test_brokerage_always_between_floor_and_cap(value: float) -> None:
    assert 5.0 <= brokerage(value, S) <= 20.0


@given(
    st.floats(min_value=1, max_value=1_000_000, allow_nan=False),
    st.floats(min_value=1, max_value=1_000_000, allow_nan=False),
    st.sampled_from(["buy", "sell"]),
)
def test_charges_never_fall_as_the_order_grows(a: float, b: float, side: str) -> None:
    lo, hi = sorted((a, b))
    small, large = order_charges(side, lo, S), order_charges(side, hi, S)  # type: ignore[arg-type]
    assert large.total >= small.total - 1e-9
    assert all(v >= 0 for v in small.as_dict().values())
