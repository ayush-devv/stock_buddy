"""WP 2.4: alert rules and the engine (edge trigger, shadow mode, weekly review)."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest

from pi.alerts import engine, run_alerts
from pi.alerts import rules as r
from pi.alerts.telegram import AlertMessage
from pi.config import load_all
from pi.ledger.book import Lot
from pi.store import db
from pi.store.clock import IST

CFG = load_all(Path(__file__).resolve().parents[1] / "config")
A = CFG.rules.alerts  # drawdown 15% / 8% vs index, vol spike 2.0, LTCG 30 days, 80%, CA 5 days
RNG = np.random.default_rng(11)


def h(prices, index, stages=(), sec=1, ticker="AAA") -> r.Holding:
    return r.Holding(sec, ticker, prices, index, tuple(stages))


# --------------------------------------------------------------------------- rules


def test_drawdown_needs_both_the_fall_and_the_lag_behind_the_index() -> None:
    assert r.drawdown_rule([h([100, 120, 90], [100, 100, 100])], A)  # -25%, 10% behind
    assert not r.drawdown_rule([h([100, 120, 95], [100, 100, 100])], A)  # only 5% behind
    assert not r.drawdown_rule([h([100, 120, 90], [100, 120, 92])], A)  # the whole market fell


def test_surveillance_stage_change_is_a_new_condition() -> None:
    [a] = r.surveillance_rule([h([1, 1], [1, 1], ["ASM-ST-1"])])
    [b] = r.surveillance_rule([h([1, 1], [1, 1], ["ASM-ST-1", "GSM-1"])])
    assert a.key != b.key and a.severity == "red"
    assert r.surveillance_rule([h([1, 1], [1, 1])]) == []


def test_risk_shift_fires_on_a_volatility_spike_only_with_enough_history() -> None:
    quiet = list(100 * np.cumprod(1 + RNG.normal(0, 0.01, 300)))
    shock = quiet[:-5] + [quiet[-6] * f for f in (1.08, 0.99, 1.09, 0.98, 1.08)]
    assert not r.risk_shift_rule([h(quiet, quiet)], A)
    assert r.risk_shift_rule([h(shock, shock)], A)
    assert not r.risk_shift_rule([h(shock[-100:], shock[-100:])], A)


def lot(acquired: date) -> Lot:
    return Lot(1, 1, "groww", 10, Decimal("1000"), acquired, acquired, "buy")


@pytest.mark.parametrize(
    ("today", "fires"),
    [(date(2025, 12, 20), True), (date(2025, 11, 1), False), (date(2026, 1, 11), False)],
)
def test_ltcg_soon_window(today: date, fires: bool) -> None:
    """Bought 10 Jan 2025 -> long-term from 11 Jan 2026."""
    out = r.ltcg_soon_rule([lot(date(2025, 1, 10))], {1: "AAA"}, today, CFG.tax, A)
    assert bool(out) is fires


def test_ltcg_headroom_threshold() -> None:
    assert r.ltcg_headroom_rule(100_000, 125_000, 2026, A)  # 80%
    assert not r.ltcg_headroom_rule(99_000, 125_000, 2026, A)


def test_corporate_action_ahead_window() -> None:
    today = date(2026, 6, 1)
    events = [
        r.CorporateEvent(1, "AAA", today + timedelta(days=3), "dividend"),
        r.CorporateEvent(2, "BBB", today + timedelta(days=6), "bonus"),
        r.CorporateEvent(3, "CCC", today - timedelta(days=1), "split"),
    ]
    assert [c.subject for c in r.ca_ahead_rule(events, today, A)] == ["AAA"]


# --------------------------------------------------------------------------- engine


class Recorder:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[str] = []
        self.fail = fail

    def send(self, m: AlertMessage) -> None:
        if self.fail:
            raise RuntimeError("telegram answered 502")
        self.sent.append(m.text())


@pytest.fixture
def con(tmp_path: Path):
    c = db.connect(tmp_path / "pi.duckdb")
    db.migrate(c, "core", db.CORE_MIGRATIONS)
    assert db.migrate(c, "alerts", run_alerts.MIGRATIONS) == 2
    yield c
    c.close()


def at(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, 19, tzinfo=IST)


DD = r.Condition("DRAWDOWN", "sec:1", "AAA", 1, {"drawdown": -0.25})
D0 = date(2026, 6, 1)  # a Monday


def run(con, day: date, conds, evaluated=("DRAWDOWN",), n=None):
    return engine.process(con, evaluated, conds, A, at(day), n, "test")


def test_an_alert_fires_once_while_its_condition_lasts_and_rearms_after(con) -> None:
    n = Recorder()
    assert [f.mode for f in run(con, D0, [DD], n=n)] == ["live"]
    assert run(con, D0 + timedelta(days=1), [DD], n=n) == []  # still true: silent
    assert run(con, D0 + timedelta(days=2), [], n=n) == []  # false: re-armed
    assert len(run(con, D0 + timedelta(days=3), [DD], n=n)) == 1  # true again: fires
    assert n.sent == ["🔴 DRAWDOWN AAA", "🔴 DRAWDOWN AAA"]


def test_a_rule_that_did_not_run_today_is_not_rearmed(con) -> None:
    run(con, D0, [DD])
    run(con, D0 + timedelta(days=1), [], evaluated=())  # e.g. prices missing today
    assert run(con, D0 + timedelta(days=2), [DD]) == []


def test_new_yellow_rules_start_in_shadow_for_20_weekdays(con) -> None:
    n = Recorder()
    first = r.Condition("LTCG_HEADROOM", "fy:2026", None, None, {})
    later = r.Condition("LTCG_HEADROOM", "fy:2027", None, None, {})
    assert [f.mode for f in run(con, D0, [first], ("LTCG_HEADROOM",), n)] == ["shadow"]
    day_20 = D0 + timedelta(days=28)  # 20 weekdays later
    assert [f.mode for f in run(con, day_20, [first, later], ("LTCG_HEADROOM",), n)] == ["live"]
    assert n.sent == ["🟡 LTCG_HEADROOM"]


def test_red_rules_are_never_in_shadow(con) -> None:
    assert run(con, D0, [DD], n=Recorder())[0].mode == "live"


def test_details_stay_local_and_only_minimal_text_is_sent(con) -> None:
    n = Recorder()
    run(con, D0, [DD], n=n)
    payload = con.execute("SELECT payload_json, security_id FROM alerts").fetchone()
    assert json.loads(payload[0]) == {"drawdown": -0.25} and payload[1] == 1
    assert "0.25" not in n.sent[0]


def test_delivery_failure_is_recorded_not_raised(con) -> None:
    run(con, D0, [DD], n=Recorder(fail=True))
    assert con.execute("SELECT delivered, error FROM alerts").fetchone() == (
        False,
        "telegram answered 502",
    )


def test_more_than_three_reds_in_a_week_raises_one_threshold_review(con) -> None:
    n = Recorder()
    for i in range(4):
        cond = r.Condition("DRAWDOWN", f"sec:{i}", f"S{i}", i, {})
        run(con, D0 + timedelta(days=i), [cond], n=n)
    run(con, D0 + timedelta(days=4), [r.Condition("DRAWDOWN", "sec:9", "S9", 9, {})], n=n)
    assert n.sent.count("🟡 THRESHOLD_REVIEW") == 1
    assert len([s for s in n.sent if s.startswith("🔴")]) == 5  # reds are never suppressed


def test_conditions_from_unlisted_rules_are_refused(con) -> None:
    with pytest.raises(ValueError, match="not marked as evaluated"):
        run(con, D0, [DD], evaluated=())
