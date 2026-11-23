"""WP 1.11: the ETF benchmark (counterfactual, own tax) and after-tax liquidation value."""

from __future__ import annotations

from datetime import date
from decimal import Decimal as D
from pathlib import Path

import pytest

from pi.config import load_all
from pi.ledger.benchmark import ETF_ID, after_tax_value, mirror, net_flows
from pi.ledger.book import Buy, Opening, Sell, build

CFG = load_all(Path(__file__).resolve().parents[1] / "config")
COSTS, TAX = CFG.costs, CFG.tax
A, B, DM = 1, 2, "groww"
D1, D2 = date(2026, 6, 1), date(2026, 6, 2)


def flat(p: str):
    return lambda d: D(p)


def test_same_day_switch_is_a_zero_net_flow() -> None:
    entries = [
        Buy(A, DM, D1, 100, D("100"), D("0"), D2),
        Sell(A, DM, D2, 100, D("110"), D("0")),
        Buy(B, DM, D2, 110, D("100"), D("0"), date(2026, 6, 3)),
    ]
    assert net_flows(entries) == {D1: D("10000.00")}
    bench = mirror(entries, flat("100"), COSTS, TAX)
    assert len(bench.entries) == 1  # it bought once and never rotated


def test_money_in_buys_whole_etf_units_with_etf_charges() -> None:
    """Rs 10,023.66 in (Rs 10,000 + charges + STT). 100 units at Rs 100 cost
    Rs 10,000 + Rs 13.66 in ETF charges (no STT on ETF purchases), leaving Rs 10.00."""
    entries = [Buy(A, DM, D1, 100, D("100"), D("13.66"), D2, stt=D("10"))]
    bench = mirror(entries, flat("100"), COSTS, TAX)
    [etf] = bench.entries
    assert (etf.qty, etf.charges, etf.stt) == (100, D("13.66"), D("0.00"))
    assert bench.cash == D("10.00")
    assert bench.book.quantity(ETF_ID) == 100


def test_money_out_sells_just_enough_etf() -> None:
    entries = [
        Buy(A, DM, D1, 100, D("100"), D("0"), D2),  # 10,000 in -> 99 units, 86.47 cash
        Sell(A, DM, D2, 50, D("100"), D("0")),  # 5,000 out
    ]
    bench = mirror(entries, lambda d: D("100") if d == D1 else D("110"), COSTS, TAX)
    first, second = bench.entries
    assert first.qty == 99 and second.qty == 45
    assert bench.book.quantity(ETF_ID) == 54
    assert D(0) <= bench.cash < D("110") and bench.shortfall == 0


def test_benchmark_tax_lives_in_its_own_world() -> None:
    """The real book's gains never enter the benchmark's realized gains."""
    # Dates must fall inside a costs.yaml schedule (Groww's starts 21 Jun 2025); the
    # benchmark refuses to guess charges for earlier periods.
    real = [
        Opening(A, DM, 1000, D("10000"), date(2025, 7, 1), date(2025, 7, 2)),
        Sell(A, DM, D2, 1000, D("200"), D("0")),  # a big real gain
    ]
    bench = mirror(real, flat("100"), COSTS, TAX)
    gains = bench.book.realized_gains(date(2026, 4, 1), date(2027, 3, 31))
    assert all("lot" in g.label for g in gains)
    assert {s.security for s in bench.book.slices} <= {ETF_ID}


def test_after_tax_value_hand_worked() -> None:
    """100 shares, cost Rs 10,000, held since Jan 2025, sold out at Rs 150 on 1 Jun 2026:
    gross 15,000; charges 33.24 (STT 15.00 of it); DP 23.60; value 14,943.16.
    Taxable gain 15,000 - 10,000 - 18.24 - 23.60 = 4,958.16 long-term, inside the
    Rs 1.25 lakh limit, so no tax."""
    book = build([Opening(A, DM, 100, D("10000"), date(2025, 1, 2), date(2025, 1, 3))], TAX)
    assert after_tax_value(book, D(0), lambda s: D("150"), D1, COSTS, TAX) == D("14943.16")


def test_outflow_exactly_equal_to_leftover_cash_sells_nothing() -> None:
    entries = [
        Buy(A, DM, D1, 100, D("100"), D("13.66"), D2, stt=D("10")),  # leaves Rs 10.00 cash
        Sell(A, DM, D2, 1, D("10"), D("0")),  # Rs 10.00 out
    ]
    bench = mirror(entries, flat("100"), COSTS, TAX)
    assert len(bench.entries) == 1 and bench.cash == D("0.00")
    assert bench.book.quantity(ETF_ID) == 100


def test_real_etf_lots_are_sold_out_at_etf_rates() -> None:
    """A real ETF lot has an ordinary security_id; refdata says it is an ETF, so selling it
    out pays 0.001% STT, not the 0.1% charged on shares."""
    etf = 7
    book = build([Opening(etf, DM, 100, D("10000"), date(2025, 1, 2), date(2025, 1, 3))], TAX)
    as_equity = after_tax_value(book, D(0), lambda s: D("150"), D1, COSTS, TAX)
    as_etf = after_tax_value(
        book, D(0), lambda s: D("150"), D1, COSTS, TAX, instrument_of=lambda s: "etf"
    )
    assert as_etf - as_equity == D("14.85")  # STT 15.00 vs 0.15


def test_after_tax_value_charges_tax_once_the_years_limit_is_used() -> None:
    book = build(
        [
            Opening(A, DM, 1, D("1"), date(2024, 1, 2), date(2024, 1, 3)),
            Sell(A, DM, date(2026, 5, 1), 1, D("125001"), D("0")),  # uses the Rs 1.25L limit
            Opening(B, DM, 100, D("10000"), date(2025, 1, 2), date(2025, 1, 3)),
        ],
        TAX,
    )
    value = after_tax_value(book, D(0), lambda s: D("150"), D1, COSTS, TAX)
    assert value == pytest.approx(D("14943.16") - D("4958.16") * D("0.13"), abs=D("0.02"))
