"""WP 1.10: tax engine. Each test states its legal basis. Together they are the case list
for the yearly CA review (implementation plan WP 1.10). Not tax advice.

Amounts are hand-worked: tax = taxable x rate, then 4% health and education cess.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from pi.config import load_all
from pi.ledger.tax import (
    CarryForward,
    RealizedGain,
    TaxScopeError,
    add_months,
    buyback_regime,
    compute_fy,
    fy_start_year,
    holding_kind,
    long_term_from,
    marginal_tax,
)

TAX = load_all(Path(__file__).resolve().parents[1] / "config").tax
approx = pytest.approx


def st(d: date, amount: float) -> RealizedGain:
    return RealizedGain(d, "short", amount)


def lt(d: date, amount: float) -> RealizedGain:
    return RealizedGain(d, "long", amount)


FY26 = date(2026, 6, 1)  # a sale date inside FY 2026-27


# --------------------------------------------------------------------------- dates


def test_financial_year_runs_april_to_march() -> None:
    assert fy_start_year(date(2026, 3, 31)) == 2025
    assert fy_start_year(date(2026, 4, 1)) == 2026


def test_exactly_twelve_months_is_still_short_term() -> None:
    """s.2(42A) proviso: listed shares are short-term if held NOT MORE than 12 months."""
    assert holding_kind(date(2025, 1, 10), date(2026, 1, 10), TAX) == "short"
    assert holding_kind(date(2025, 1, 10), date(2026, 1, 11), TAX) == "long"
    assert long_term_from(date(2025, 1, 10), TAX) == date(2026, 1, 11)


def test_leap_day_purchase_is_clamped() -> None:
    """A lot bought on 29 Feb has no anniversary in a non-leap year; 28 Feb is used."""
    assert add_months(date(2024, 2, 29), 12) == date(2025, 2, 28)
    assert holding_kind(date(2024, 2, 29), date(2025, 2, 28), TAX) == "short"
    assert holding_kind(date(2024, 2, 29), date(2025, 3, 1), TAX) == "long"


def test_same_day_buy_and_sell_is_speculative() -> None:
    """s.43(5): a delivery trade squared off the same day is speculative business income."""
    assert holding_kind(date(2026, 6, 1), date(2026, 6, 1), TAX) == "speculative"


def test_pre_2018_lots_are_out_of_scope() -> None:
    """s.112A grandfathering (FMV on 31 Jan 2018) is not modelled."""
    with pytest.raises(TaxScopeError, match="grandfathered"):
        holding_kind(date(2018, 1, 31), date(2026, 6, 1), TAX)


@pytest.mark.parametrize(
    ("announced", "bought_back", "regime"),
    [
        (date(2019, 6, 1), date(2019, 9, 1), "capital_gains"),  # announced before the 2019 change
        (date(2019, 8, 1), date(2019, 10, 1), "exempt_s10_34a"),  # s.115QA on the company
        (date(2024, 8, 1), date(2024, 10, 15), "deemed_dividend"),  # Finance (No. 2) Act 2024
        (date(2026, 3, 1), date(2026, 4, 20), "capital_gains"),  # Finance Act 2026 (secondary)
    ],
)
def test_buyback_regime(announced: date, bought_back: date, regime: str) -> None:
    assert buyback_regime(announced, bought_back, TAX) == regime


# --------------------------------------------------------------------------- one year


def test_stcg_at_20_percent_plus_cess() -> None:
    """s.111A: 20% from 23 Jul 2024; no 87A rebate against it under the new regime."""
    r = compute_fy([st(FY26, 10_000)], TAX, 2026)
    assert r.tax_before_cess == approx(2_000)
    assert r.total_tax == approx(2_080)


def test_ltcg_above_the_annual_exemption() -> None:
    """s.112A: 12.5% on LTCG above Rs 1.25 lakh a year."""
    r = compute_fy([lt(FY26, 150_000)], TAX, 2026)
    assert r.exemption_used == approx(125_000)
    assert r.total_tax == approx(25_000 * 0.125 * 1.04)  # 3,250


def test_ltcg_within_the_exemption_is_tax_free() -> None:
    assert compute_fy([lt(FY26, 100_000)], TAX, 2026).total_tax == 0.0


def test_exemption_is_annual_so_a_july_2024_sale_gets_1_25_lakh() -> None:
    """Finance (No. 2) Act 2024: the Rs 1.25 lakh limit applies to FY 2024-25 as a whole,
    even though the rate on this 10 Jul 2024 sale is still the old 10%."""
    r = compute_fy([lt(date(2024, 7, 10), 120_000)], TAX, 2024)
    assert r.exemption_limit == approx(125_000)
    assert r.total_tax == 0.0
    assert r.rules_used["ltcg_exemption_inr"] == "secondary"  # flagged for the CA review


def test_in_a_split_year_a_loss_offsets_the_higher_rate_gain_first() -> None:
    """FY 2024-25: STCG before 23 Jul at 15%, after at 20%. Set-off is applied where it
    saves the most tax."""
    gains = [
        st(date(2024, 6, 1), 10_000),  # 15%
        st(date(2024, 9, 1), 10_000),  # 20%
        st(date(2024, 10, 1), -10_000),  # loss
    ]
    r = compute_fy(gains, TAX, 2024)
    assert r.stcg_taxable[0.20] == approx(0)
    assert r.stcg_taxable[0.15] == approx(10_000)
    assert r.total_tax == approx(1_560)


def test_short_term_loss_can_offset_ltcg() -> None:
    """s.70(2): a short-term capital loss can be set off against any capital gain."""
    r = compute_fy([st(FY26, -20_000), lt(FY26, 150_000)], TAX, 2026)
    assert r.total_tax == approx(5_000 * 0.125 * 1.04)  # 130,000 - 125,000 exempt = 5,000


def test_long_term_loss_cannot_offset_stcg_and_is_carried_forward() -> None:
    """s.70(3) and s.74: a long-term loss is set off only against LTCG."""
    r = compute_fy([lt(FY26, -10_000), st(FY26, 10_000)], TAX, 2026)
    assert r.total_tax == approx(2_080)
    assert r.carry_forward_out == CarryForward(short_loss=0, long_loss=10_000)


def test_brought_forward_loss_is_used_before_the_exemption() -> None:
    """s.74 set-off comes before the s.112A exemption, so a carried loss is used up even
    when the gain would have been exempt anyway. To confirm at the CA review."""
    r = compute_fy([lt(FY26, 100_000)], TAX, 2026, CarryForward(long_loss=50_000))
    assert r.total_tax == 0.0
    assert r.carry_forward_out.long_loss == 0.0
    assert r.exemption_used == approx(50_000)


def test_brought_forward_short_loss_covers_stcg_then_ltcg() -> None:
    r = compute_fy(
        [st(FY26, 10_000), lt(FY26, 140_000)], TAX, 2026, CarryForward(short_loss=25_000)
    )
    # 10,000 STCG cleared; 15,000 more off LTCG -> 125,000, all exempt.
    assert r.total_tax == 0.0
    assert r.carry_forward_out.short_loss == 0.0


def test_speculative_income_is_returned_for_slab_tax_not_computed() -> None:
    r = compute_fy([RealizedGain(FY26, "speculative", 500)], TAX, 2026)
    assert r.speculative_income == 500 and r.needs_slab
    assert r.total_tax == 0.0


def test_sales_outside_the_year_are_refused() -> None:
    with pytest.raises(ValueError, match="outside FY 2026"):
        compute_fy([st(date(2026, 3, 31), 1)], TAX, 2026)


def test_negative_carry_forward_is_refused() -> None:
    with pytest.raises(ValueError):
        CarryForward(short_loss=-1)


# --------------------------------------------------------------------------- hurdle input


def test_marginal_tax_of_a_gain_counts_the_shared_headroom() -> None:
    """The Rs 1.25 lakh limit is shared by every real equity sale in the year, including
    sales of real ETF lots (architecture §11.1, E6). The counterfactual ETF benchmark
    (WP 1.11) is never included here."""
    existing = [lt(FY26, 120_000)]  # e.g. real gains already realized this year
    extra = marginal_tax(existing, [lt(FY26, 20_000)], TAX, 2026)
    assert extra == approx(15_000 * 0.125 * 1.04)  # 1,950: only 5,000 of headroom was left


def test_marginal_tax_is_negative_when_a_sale_realizes_a_useful_loss() -> None:
    """The loss case in §11.1: selling at a loss can lower this year's tax."""
    extra = marginal_tax([st(FY26, 10_000)], [st(FY26, -5_000)], TAX, 2026)
    assert extra == approx(1_040 - 2_080)  # -1,040
