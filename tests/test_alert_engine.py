"""WP 2.4: alert rules and the engine (edge trigger, retries, shadow mode, weekly review)."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest

from pi.alerts import engine, run_alerts
from pi.alerts import rules as r
from pi.alerts.telegram import AlertContentError, AlertMessage
from pi.config import load_all
from pi.ledger.book import Lot
from pi.store import db
from pi.store.clock import IST

CFG = load_all(Path(__file__).resolve().parents[1] / "config")
A = CFG.rules.alerts  # drawdown 15% / 8% vs index, vol spike 2.0, LTCG 30 days, 80%, CA 5 days
RNG = np.random.default_rng(11)


def h(prices, index, stages=(), sec=1, ticker="AAA", history=()) -> r.Holding:
    return r.Holding(sec, ticker, prices, index, tuple(stages), history)


# --------------------------------------------------------------------------- rules


def test_drawdown_needs_both_the_fall_and_the_lag_behind_the_index() -> None:
    assert r.drawdown_rule([h([100, 120, 90], [100, 100, 100])], A).conditions  # -25%, 10% behind
    assert not r.drawdown_rule([h([100, 120, 95], [100, 100, 100])], A).conditions
    assert not r.drawdown_rule([h([100, 120, 90], [100, 120, 92])], A).conditions  # market fell


def test_misaligned_data_means_not_evaluated() -> None:
    res = r.drawdown_rule([h([100, 120, 90], [100, 100])], A)
    assert res.evaluated == frozenset() and res.conditions == ()


def test_surveillance_stage_change_is_a_new_key_in_the_same_scope() -> None:
    [a] = r.surveillance_rule([h([1, 1], [1, 1], ["ASM-ST-1"])]).conditions
    [b] = r.surveillance_rule([h([1, 1], [1, 1], ["ASM-ST-1", "GSM-1"])]).conditions
    assert a.key != b.key and a.scope == b.scope and a.severity == "red"
    quiet = r.surveillance_rule([h([1, 1], [1, 1])])
    assert quiet.conditions == () and quiet.evaluated == {"sec:1"}


def test_risk_shift_uses_full_history_not_just_since_purchase() -> None:
    quiet = list(100 * np.cumprod(1 + RNG.normal(0, 0.01, 300)))
    shock = quiet[:-5] + [quiet[-6] * f for f in (1.08, 0.99, 1.09, 0.98, 1.08)]
    bought_last_week = shock[-5:]
    assert not r.risk_shift_rule(
        [h(bought_last_week, bought_last_week, history=quiet)], A
    ).conditions
    assert r.risk_shift_rule([h(bought_last_week, bought_last_week, history=shock)], A).conditions
    assert r.risk_shift_rule([h([1, 1], [1, 1], history=shock[-100:])], A).evaluated == frozenset()


def lot(lot_id: int, acquired: date, qty: int = 10) -> Lot:
    return Lot(lot_id, 1, "groww", qty, Decimal("1000"), acquired, acquired, "buy")


@pytest.mark.parametrize(
    ("today", "fires"),
    [(date(2025, 12, 20), True), (date(2025, 11, 1), False), (date(2026, 1, 11), False)],
)
def test_ltcg_soon_window(today: date, fires: bool) -> None:
    """Bought 10 Jan 2025 -> long-term from 11 Jan 2026."""
    out = r.ltcg_soon_rule([lot(1, date(2025, 1, 10))], {1: "AAA"}, today, CFG.tax, A)
    assert bool(out.conditions) is fires


def test_ltcg_soon_keys_survive_lot_renumbering_and_group_same_day_lots() -> None:
    today = date(2025, 12, 20)
    a = r.ltcg_soon_rule(
        [lot(1, date(2025, 1, 10), 4), lot(2, date(2025, 1, 10), 6)], {1: "AAA"}, today, CFG.tax, A
    )
    b = r.ltcg_soon_rule([lot(7, date(2025, 1, 10), 10)], {1: "AAA"}, today, CFG.tax, A)
    [ca], [cb] = a.conditions, b.conditions
    assert ca.key == cb.key == "ltcg:1:groww:2026-01-11"
    assert ca.payload["qty"] == 10


def test_ltcg_headroom_threshold() -> None:
    assert r.ltcg_headroom_rule(100_000, 125_000, 2026, A).conditions  # 80%
    assert not r.ltcg_headroom_rule(99_000, 125_000, 2026, A).conditions


def test_corporate_action_ahead_window() -> None:
    today = date(2026, 6, 1)
    events = [
        r.CorporateEvent(1, "AAA", today + timedelta(days=3), "dividend"),
        r.CorporateEvent(2, "BBB", today + timedelta(days=6), "bonus"),
        r.CorporateEvent(3, "CCC", today - timedelta(days=1), "split"),
    ]
    assert [c.subject for c in r.ca_ahead_rule(events, today, A).conditions] == ["AAA"]


def test_a_rule_cannot_report_conditions_outside_what_it_evaluated() -> None:
    c = r.Condition("DRAWDOWN", "sec:1", "sec:1", "AAA", 1)
    with pytest.raises(ValueError, match="outside"):
        r.RuleResult("DRAWDOWN", frozenset(), (c,))


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


D0 = date(2026, 6, 1)  # a Monday
DD = r.Condition("DRAWDOWN", "sec:1", "sec:1", "AAA", 1, {"drawdown": -0.25})


def dd_result(true: bool, evaluated=("sec:1",)) -> r.RuleResult:
    return r.RuleResult("DRAWDOWN", frozenset(evaluated), (DD,) if true else ())


def run(con, day: date, *results, n=None):
    return engine.process(con, results, A, at(day), n, "test")


def test_an_alert_fires_once_while_its_condition_lasts_and_rearms_after(con) -> None:
    n = Recorder()
    assert [f.mode for f in run(con, D0, dd_result(True), n=n)] == ["live"]
    assert run(con, D0 + timedelta(days=1), dd_result(True), n=n) == []  # still true: silent
    assert run(con, D0 + timedelta(days=2), dd_result(False), n=n) == []  # false: re-armed
    assert len(run(con, D0 + timedelta(days=3), dd_result(True), n=n)) == 1
    assert n.sent == ["🔴 DRAWDOWN AAA", "🔴 DRAWDOWN AAA"]


def test_a_data_gap_does_not_rearm_and_refire(con) -> None:
    n = Recorder()
    run(con, D0, dd_result(True), n=n)
    run(con, D0 + timedelta(days=1), dd_result(False, evaluated=()), n=n)  # index data missing
    run(con, D0 + timedelta(days=2), dd_result(True), n=n)
    assert n.sent == ["🔴 DRAWDOWN AAA"]


def test_a_rule_that_did_not_run_today_is_not_rearmed(con) -> None:
    n = Recorder()
    run(con, D0, dd_result(True), n=n)
    run(con, D0 + timedelta(days=1), n=n)  # no results at all today
    assert run(con, D0 + timedelta(days=2), dd_result(True), n=n) == []
    assert n.sent == ["🔴 DRAWDOWN AAA"]


def test_an_undelivered_alert_is_retried_until_it_goes_out(con) -> None:
    run(con, D0, dd_result(True), n=Recorder(fail=True))
    ok = Recorder()
    [f] = run(con, D0 + timedelta(days=1), dd_result(True), n=ok)
    assert f.retry and f.delivered and ok.sent == ["🔴 DRAWDOWN AAA"]
    assert run(con, D0 + timedelta(days=2), dd_result(True), n=ok) == []  # sent once only
    assert con.execute("SELECT count(*), bool_and(delivered) FROM alerts").fetchone() == (1, True)


def test_alerts_pending_before_telegram_was_set_up_go_out_once_it_is(con) -> None:
    run(con, D0, dd_result(True), n=None)
    n = Recorder()
    run(con, D0 + timedelta(days=1), dd_result(True), n=n)
    assert n.sent == ["🔴 DRAWDOWN AAA"]


HEAD = r.Condition("LTCG_HEADROOM", "fy:2026", "fy:2026", None, None, {})


def head(true: bool = True) -> r.RuleResult:
    return r.RuleResult("LTCG_HEADROOM", frozenset({"fy:2026"}), (HEAD,) if true else ())


def test_yellow_rules_start_in_shadow_and_send_on_their_first_live_day(con) -> None:
    n = Recorder()
    assert [f.mode for f in run(con, D0, head(), n=n)] == ["shadow"]
    assert run(con, D0 + timedelta(days=7), head(), n=n) == []  # still in shadow
    live_day = D0 + timedelta(days=28)  # 20 trading days after the rule was first seen
    [f] = run(con, live_day, head(), n=n)
    assert (f.mode, f.delivered) == ("live", True)
    assert n.sent == ["🟡 LTCG_HEADROOM"]
    modes = sorted(m for (m,) in con.execute("SELECT mode FROM alerts").fetchall())
    assert modes == ["live", "shadow"]  # the shadow record is kept for calibration


def test_holidays_extend_the_shadow_period(con) -> None:
    run(con, D0, head())
    day = D0 + timedelta(days=28)
    holidays = frozenset({D0 + timedelta(days=2)})
    assert engine.process(con, [head()], A, at(day), Recorder(), "t", holidays) == []


def test_red_rules_are_never_in_shadow(con) -> None:
    assert run(con, D0, dd_result(True), n=Recorder())[0].mode == "live"


def test_details_stay_local_and_only_minimal_text_is_sent(con) -> None:
    n = Recorder()
    run(con, D0, dd_result(True), n=n)
    payload, sec = con.execute("SELECT payload_json, security_id FROM alerts").fetchone()
    assert json.loads(payload) == {"drawdown": -0.25} and sec == 1
    assert "0.25" not in n.sent[0]


def test_a_bad_subject_raises_instead_of_being_logged_as_a_failed_send(con) -> None:
    bad = r.Condition("DRAWDOWN", "sec:1", "sec:1", "bad ticker!", 1)
    with pytest.raises(AlertContentError):
        run(con, D0, r.RuleResult("DRAWDOWN", frozenset({"sec:1"}), (bad,)), n=Recorder())


def test_more_than_three_market_reds_in_a_week_raise_one_review(con) -> None:
    n = Recorder()
    for i in range(5):
        c = r.Condition("DRAWDOWN", f"sec:{i}", f"sec:{i}", f"S{i}", i)
        run(
            con,
            D0 + timedelta(days=i),
            r.RuleResult("DRAWDOWN", frozenset({f"sec:{i}"}), (c,)),
            n=n,
        )
    assert n.sent.count("🟡 THRESHOLD_REVIEW") == 1
    assert len([s for s in n.sent if s.startswith("🔴")]) == 5  # reds are never suppressed


def test_run_alerts_do_not_count_towards_the_review(con) -> None:
    n = Recorder()
    for i in range(5):  # a week of missing files
        run_alerts.fire(con, AlertMessage("red", "DATA_MISSING", f"2026-06-0{i + 1}"), n, at(D0))
    run(con, D0, dd_result(True), n=n)
    assert "🟡 THRESHOLD_REVIEW" not in n.sent


def test_each_rule_reports_once(con) -> None:
    with pytest.raises(ValueError, match="once"):
        run(con, D0, dd_result(True), dd_result(False))
