"""WP 1.8: ledger core. Made-up trades with hand-worked lots and gains."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal as D
from fractions import Fraction
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from pi.config import load_all
from pi.ledger.book import (
    Bonus,
    Buy,
    Buyback,
    Demerger,
    LedgerError,
    Merger,
    Opening,
    Sell,
    Split,
    build,
)
from pi.ledger.tax import compute_fy

TAX = load_all(Path(__file__).resolve().parents[1] / "config").tax
A, B = 1, 2  # security ids
DM = "groww"


def buy(d: date, qty: int, price: str, charges: str = "0", credit: date | None = None, sec=A):
    return Buy(sec, DM, d, qty, D(price), D(charges), credit or d + timedelta(days=1))


def sell(d: date, qty: int, price: str, charges: str = "0", dp: str = "0", sec=A):
    return Sell(sec, DM, d, qty, D(price), D(charges), D(dp))


def conserved(book) -> bool:
    open_cost = sum((lot.cost for lot in book.lots), D(0))
    sold_cost = sum((s.cost for s in book.slices), D(0))
    return book.cost_in == open_cost + sold_cost


# --------------------------------------------------------------------------- buys and sells


def test_fifo_partial_sale_hand_worked() -> None:
    book = build(
        [
            buy(date(2026, 1, 5), 10, "100", "5"),  # cost 1,005
            buy(date(2026, 2, 2), 10, "120", "5"),  # cost 1,205
            sell(date(2026, 6, 1), 15, "130", "6", dp="23.60"),
        ],
        TAX,
    )
    s1, s2 = book.slices
    assert (s1.qty, s1.cost, s1.proceeds, s1.expenses) == (10, D("1005"), D("1300"), D("19.73"))
    assert (s2.qty, s2.cost, s2.proceeds, s2.expenses) == (5, D("602.50"), D("650"), D("9.87"))
    assert s1.gain == D("275.27") and s2.gain == D("37.63")
    assert {s1.kind, s2.kind} == {"short"}
    [left] = book.open_lots(A)
    assert (left.qty, left.cost) == (5, D("602.50"))
    assert conserved(book)


def test_stt_is_never_part_of_cost() -> None:
    b = Buy(A, DM, date(2026, 1, 5), 10, D("100"), D("5"), date(2026, 1, 6), stt=D("1"))
    assert build([b], TAX).open_lots(A)[0].cost == D("1005")


def test_dp_charge_is_an_expense_only_if_the_rule_says_so() -> None:
    rules = [
        r.model_copy(update={"value": False}) if r.id == "dp_charge_is_transfer_expense" else r
        for r in TAX.rules
    ]
    no_dp = TAX.model_copy(update={"rules": rules})
    entries = [buy(date(2026, 1, 5), 10, "100"), sell(date(2026, 6, 1), 10, "110", "5", dp="23.60")]
    assert build(entries, TAX).slices[0].expenses == D("28.60")
    assert build(entries, no_dp).slices[0].expenses == D("5")


def test_long_term_only_after_more_than_twelve_months() -> None:
    on_anniversary = build([buy(date(2025, 1, 10), 1, "1"), sell(date(2026, 1, 10), 1, "2")], TAX)
    day_after = build([buy(date(2025, 1, 10), 1, "1"), sell(date(2026, 1, 11), 1, "2")], TAX)
    assert on_anniversary.slices[0].kind == "short"
    assert day_after.slices[0].kind == "long"


def test_same_day_sale_matches_todays_buy_first_as_speculative() -> None:
    d = date(2026, 6, 1)
    book = build([buy(date(2025, 1, 2), 10, "50"), buy(d, 5, "60"), sell(d, 5, "61")], TAX)
    [s] = book.slices
    assert s.kind == "speculative" and s.acquired == d
    assert book.quantity(A) == 10 and book.open_lots(A)[0].acquired == date(2025, 1, 2)


def test_fifo_follows_the_date_shares_entered_the_demat() -> None:
    """CBDT Circular 768: FIFO by entry into the demat. A bonus credited on 3 Mar enters
    after shares bought on the ex-date and credited on 2 Mar."""
    book = build(
        [
            buy(date(2025, 1, 2), 10, "100"),
            Bonus(
                A,
                date(2026, 3, 1),
                Fraction(1),
                allotted_on=date(2026, 3, 2),
                credited_on=date(2026, 3, 3),
            ),
            buy(date(2026, 3, 1), 4, "60", credit=date(2026, 3, 2)),
            sell(date(2026, 3, 10), 14, "70"),
        ],
        TAX,
    )
    assert [s.qty for s in book.slices] == [10, 4]
    assert [s.acquired for s in book.slices] == [date(2025, 1, 2), date(2026, 3, 1)]
    assert book.open_lots(A)[0].cost == D("0")  # only the bonus lot remains


def test_dp_charge_may_ride_on_only_one_sell_per_stock_and_day() -> None:
    d = date(2026, 6, 1)
    entries = [buy(date(2026, 1, 5), 10, "100"), sell(d, 4, "110", dp="23.60"), sell(d, 6, "111")]
    assert len(build(entries, TAX).slices) == 2  # two orders, one DP charge: fine
    with pytest.raises(LedgerError, match="DP charge recorded twice"):
        build([*entries[:2], sell(d, 6, "111", dp="23.60")], TAX)


def test_selling_more_than_held_never_invents_a_lot() -> None:
    with pytest.raises(LedgerError, match="only 10 held"):
        build([buy(date(2026, 1, 5), 10, "100"), sell(date(2026, 2, 1), 11, "100")], TAX)


def test_credited_quantity_and_trade_history_for_the_guard() -> None:
    book = build([buy(date(2026, 6, 1), 10, "100"), buy(date(2026, 6, 5), 5, "100")], TAX)
    assert book.credited_qty(A, DM, date(2026, 6, 5)) == 10  # today's buy arrives tomorrow
    assert book.credited_qty(A, DM, date(2026, 6, 6)) == 15
    assert book.bought_on(A, DM, date(2026, 6, 5)) == 5
    assert book.last_trade(A, "BUY") == date(2026, 6, 5)
    assert book.last_trade(A, "SELL") is None


def test_opening_lots_carry_their_own_dates_and_cost() -> None:
    book = build(
        [
            Opening(A, DM, 7, D("700"), date(2024, 3, 1), date(2024, 3, 4)),
            sell(date(2026, 6, 1), 7, "150"),
        ],
        TAX,
    )
    assert book.slices[0].kind == "long" and book.slices[0].gain == D("350")


# --------------------------------------------------------------------------- corporate actions


def test_split_multiplies_quantity_and_keeps_cost_and_dates() -> None:
    book = build([buy(date(2025, 1, 2), 10, "100"), Split(A, date(2026, 1, 5), Fraction(5))], TAX)
    [lot] = book.open_lots(A)
    assert (lot.qty, lot.cost, lot.acquired) == (50, D("1000"), date(2025, 1, 2))


def test_fractional_consolidation_is_refused() -> None:
    with pytest.raises(LedgerError, match="fractional"):
        build([buy(date(2025, 1, 2), 15, "10"), Split(A, date(2026, 1, 5), Fraction(1, 10))], TAX)


def test_bonus_lot_has_nil_cost_and_its_own_holding_period() -> None:
    """Bonus shares: cost nil, holding period from allotment (s.55(2)(aa), s.2(42A))."""
    book = build(
        [
            buy(date(2025, 1, 2), 10, "100"),
            Bonus(A, date(2025, 9, 1), Fraction(1), date(2025, 9, 2), date(2025, 9, 3)),
            sell(date(2026, 3, 2), 20, "60"),
        ],
        TAX,
    )
    original, bonus = book.slices
    assert (original.kind, original.cost) == ("long", D("1000"))
    assert (bonus.kind, bonus.cost, bonus.gain) == ("short", D("0"), D("600"))
    assert conserved(book)


def test_demerger_moves_the_announced_cost_share_and_keeps_dates() -> None:
    """s.49(2C)/(2D): cost apportioned by the announced ratio; holding period of the
    resulting shares includes the original holding."""
    book = build(
        [
            buy(date(2025, 1, 2), 10, "100"),
            Demerger(A, B, date(2026, 2, 1), Fraction(1), Fraction(3, 10), date(2026, 2, 4)),
        ],
        TAX,
    )
    [parent], [child] = book.open_lots(A), book.open_lots(B)
    assert parent.cost == D("700") and child.cost == D("300")
    assert child.qty == 10 and child.acquired == date(2025, 1, 2)
    assert conserved(book)


def test_merger_converts_lots_with_cost_and_dates() -> None:
    book = build(
        [
            buy(date(2025, 1, 2), 10, "100"),
            Merger(A, B, date(2026, 2, 1), Fraction(2), date(2026, 2, 3)),
        ],
        TAX,
    )
    assert book.quantity(A) == 0
    [lot] = book.open_lots(B)
    assert (lot.qty, lot.cost, lot.acquired) == (20, D("1000"), date(2025, 1, 2))


@pytest.mark.parametrize(
    ("announced", "bought_back", "kind", "slab", "taxable"),
    [
        (date(2020, 1, 1), date(2020, 2, 1), "exempt", False, False),  # s.10(34A)
        (date(2024, 10, 1), date(2024, 11, 1), "long", True, True),  # deemed dividend
        (date(2026, 4, 1), date(2026, 5, 1), "long", False, True),  # capital gains again
    ],
)
def test_buyback_follows_its_regime(announced, bought_back, kind, slab, taxable) -> None:
    book = build(
        [
            Opening(A, DM, 10, D("1000"), date(2019, 1, 1), date(2019, 1, 2)),
            Buyback(A, DM, announced, bought_back, 10, D("150")),
        ],
        TAX,
    )
    [s] = book.slices
    assert s.kind == kind
    assert bool(book.slab_income) is slab
    if slab:  # deemed dividend: proceeds are slab income, the cost becomes a capital loss
        assert book.slab_income[0][2] == D("1500") and s.gain == D("-1000")
    year = bought_back.year if bought_back.month >= 4 else bought_back.year - 1
    assert bool(book.realized_gains(date(year, 4, 1), date(year + 1, 3, 31))) is taxable


def test_realized_gains_feed_the_tax_engine() -> None:
    book = build([buy(date(2026, 4, 2), 100, "100"), sell(date(2026, 9, 1), 100, "200")], TAX)
    fy = compute_fy(book.realized_gains(date(2026, 4, 1), date(2027, 3, 31)), TAX, 2026)
    assert fy.total_tax == pytest.approx(10_000 * 0.20 * 1.04)


# --------------------------------------------------------------------------- properties

ops = st.lists(
    st.tuples(
        st.sampled_from(["buy", "buy", "sell", "split", "bonus"]),
        st.integers(1, 40),
        st.integers(100, 100_000),  # price in paise
    ),
    min_size=1,
    max_size=25,
)


@settings(max_examples=150, deadline=None)
@given(ops)
def test_lots_always_add_up_and_cost_is_conserved(seq) -> None:
    entries, expected, d = [], 0, date(2025, 1, 1)
    for i, (op, qty, paise) in enumerate(seq):
        d = d + timedelta(days=3)
        price = D(paise) / 100
        if op == "buy":
            entries.append(buy(d, qty, str(price), str(D(i) / 7)))
            expected += qty
        elif op == "sell" and expected:
            q = min(qty, expected)
            entries.append(sell(d, q, str(price), "1.11"))
            expected -= q
        elif op == "split" and expected:
            entries.append(Split(A, d, Fraction(2)))
            expected *= 2
        elif op == "bonus" and expected:
            entries.append(Bonus(A, d, Fraction(1), d, d + timedelta(days=1)))
            expected *= 2
    book = build(entries, TAX)
    assert book.quantity(A) == expected
    assert all(lot.qty >= 0 and lot.cost >= 0 for lot in book.lots)
    assert conserved(book)
