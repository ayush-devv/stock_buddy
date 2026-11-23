"""WP 1.13: guard rules (architecture §10). One or more tests per rule."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from pi.config import RestrictedEntry, load_all
from pi.guard.rules import (
    TradeContext,
    check,
    latest_quarter_end,
    trading_window_closed,
)
from pi.store.clock import IST

RULES = load_all(Path(__file__).resolve().parents[1] / "config").rules.guard
AT = datetime(2026, 11, 23, 19, tzinfo=IST)
DP = RestrictedEntry(
    security_id="1",
    basis="designated_person",
    window="quarter_end_to_results_plus_48h",
    contra_trade_months=6,
    preclearance_threshold_inr=100_000,
)
UPSI = RestrictedEntry(security_id="1", basis="holds_upsi", window="always", contra_trade_months=6)


def ctx(**kw) -> TradeContext:
    base = TradeContext(security_id=1, action="SELL", qty=10, price=500.0, at=AT, credited_qty=10)
    return replace(base, **kw)


def rules_of(result) -> list[tuple[str, str]]:
    return [(f.rule, f.outcome) for f in result.findings]


def test_an_ordinary_trade_passes() -> None:
    assert check(ctx(), RULES).verdict == "pass"
    assert check(ctx(action="BUY", credited_qty=None), RULES).verdict == "pass"


# --------------------------------------------------------------------------- insider (§10.1)


def test_unrestricted_stocks_ignore_window_and_contra() -> None:
    r = check(ctx(window_closed=True, last_opposite_trade=date(2026, 11, 1)), RULES)
    assert r.verdict == "pass"


def test_closed_window_blocks() -> None:
    r = check(ctx(restricted=(DP,), window_closed=True), RULES)
    assert ("trading_window", "block") in rules_of(r)


def test_unknown_window_state_fails_safe() -> None:
    r = check(ctx(restricted=(DP,), window_closed=None), RULES)
    assert r.blocked and "unknown" in r.findings[0].reason


def test_contra_trade_inside_six_months_blocks() -> None:
    inside = check(
        ctx(restricted=(DP,), window_closed=False, last_opposite_trade=date(2026, 6, 1)), RULES
    )
    assert ("contra_trade", "block") in rules_of(inside)


def test_contra_period_includes_the_anniversary_day() -> None:
    """PIT Schedule B: no opposite trade 'during the next six months following the prior
    transaction'. Counted from the day after, a 23 May trade's period runs through 23 Nov,
    so 23 Nov is still blocked (fail-safe) and 24 Nov is clear."""
    kw = {"restricted": (DP,), "window_closed": False, "last_opposite_trade": date(2026, 5, 23)}
    assert check(ctx(**kw), RULES).blocked  # AT is 23 Nov 2026
    assert check(ctx(at=datetime(2026, 11, 24, 10, tzinfo=IST), **kw), RULES).verdict == "pass"


def test_contra_uses_the_ist_date_not_utc() -> None:
    """00:30 IST on 24 Nov is still 23 Nov in UTC; the IST date must be used."""
    kw = {"restricted": (DP,), "window_closed": False, "last_opposite_trade": date(2026, 5, 23)}
    at_utc = datetime(2026, 11, 24, 0, 30, tzinfo=IST).astimezone(UTC)
    assert at_utc.date() == date(2026, 11, 23)
    assert check(ctx(at=at_utc, **kw), RULES).verdict == "pass"


def test_window_always_blocks_regardless_of_the_quarter() -> None:
    always = DP.model_copy(update={"window": "always"})
    assert check(ctx(restricted=(always,), window_closed=False), RULES).blocked


def test_value_above_threshold_needs_preclearance() -> None:
    r = check(ctx(restricted=(DP,), window_closed=False, qty=300, credited_qty=300), RULES)
    assert r.verdict == "needs_preclearance"  # 300 x 500 = 1.5 lakh > 1 lakh


def test_holding_upsi_blocks_any_action() -> None:
    for action in ("BUY", "SELL"):
        assert check(ctx(action=action, restricted=(UPSI,)), RULES).blocked


def test_window_closes_at_quarter_end_until_48h_after_results() -> None:
    results = datetime(2026, 10, 20, 15, tzinfo=IST)
    assert latest_quarter_end(date(2026, 10, 21)) == date(2026, 9, 30)
    assert latest_quarter_end(date(2026, 2, 1)) == date(2025, 12, 31)
    assert trading_window_closed(datetime(2026, 10, 5, tzinfo=IST), None, 48)  # not declared
    assert trading_window_closed(datetime(2026, 10, 22, 14, tzinfo=IST), results, 48)
    assert not trading_window_closed(datetime(2026, 10, 22, 15, tzinfo=IST), results, 48)


# --------------------------------------------------------------------------- surveillance (§10.2)


def test_gsm_stage_2_blocks_buying_but_not_selling() -> None:
    assert check(ctx(action="BUY", surveillance=("GSM-2",)), RULES).blocked
    sell = check(ctx(surveillance=("GSM-2",)), RULES)
    assert sell.verdict == "warn"


def test_gsm_stage_3_selling_is_weekly_only() -> None:
    assert ("gsm_weekly", "warn") in rules_of(check(ctx(surveillance=("GSM-3",)), RULES))


def test_other_surveillance_stages_warn() -> None:
    r = check(ctx(action="BUY", surveillance=("ASM-LT-1", "GSM-1")), RULES)
    assert r.verdict == "warn" and len(r.findings) == 2


@pytest.mark.parametrize("stage", ["GSM-II", "GSM Stage 2", "gsm-2", "ESM-3", ""])
def test_unrecognised_stage_strings_raise(stage: str) -> None:
    with pytest.raises(ValueError, match="unrecognised surveillance stage"):
        check(ctx(action="BUY", surveillance=(stage,)), RULES)


def test_trade_for_trade_forbids_same_day_reversal() -> None:
    assert check(ctx(series="BE", bought_today_qty=5), RULES).blocked
    assert check(ctx(series="BE"), RULES).verdict == "pass"  # delivery sale is fine


def test_same_day_sale_in_eq_warns_speculative() -> None:
    r = check(ctx(bought_today_qty=5), RULES)
    assert rules_of(r) == [("same_day", "warn")]


@pytest.mark.parametrize(
    ("kw", "rule"),
    [
        ({"at_lower_circuit": True}, "lower_circuit"),
        ({"band_cut": True}, "band_cut"),
        ({"action": "BUY", "at_upper_circuit": True, "credited_qty": None}, "upper_circuit"),
    ],
)
def test_circuit_and_band_conditions_mark_may_not_fill(kw: dict, rule: str) -> None:
    assert rules_of(check(ctx(**kw), RULES)) == [(rule, "warn")]


# --------------------------------------------------------------------------- holdings


def test_sell_is_limited_to_credited_quantity() -> None:
    r = check(ctx(qty=15, credited_qty=10), RULES)
    assert r.blocked and "Only 10" in r.findings[0].reason


def test_unknown_credited_quantity_blocks_a_sell() -> None:
    assert check(ctx(credited_qty=None), RULES).blocked


def test_sme_quantity_must_be_whole_lots() -> None:
    assert (
        check(ctx(series="SM", qty=1200, credited_qty=1200, lot_size=1200), RULES).verdict == "pass"
    )
    assert check(ctx(series="SM", qty=600, credited_qty=600, lot_size=1200), RULES).blocked
    assert check(ctx(series="ST", lot_size=None), RULES).blocked


def test_bad_inputs_are_refused() -> None:
    with pytest.raises(ValueError):
        check(ctx(qty=0), RULES)


def test_verdict_is_the_strongest_finding() -> None:
    r = check(
        ctx(
            restricted=(DP,),
            window_closed=True,
            surveillance=("ASM-ST-1",),
            qty=300,
            credited_qty=300,
        ),
        RULES,
    )
    assert {f.outcome for f in r.findings} == {"block", "needs_preclearance", "warn"}
    assert r.verdict == "block"
