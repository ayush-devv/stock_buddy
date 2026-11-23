"""WP 1.1: trading calendar and internal security IDs."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from pathlib import Path

import duckdb
import pytest
from pydantic import ValidationError

from pi.config import CalendarConfig
from pi.refdata import securities as sec
from pi.refdata.calendar import (
    clean_streak,
    expected_trading_days,
    missing_days,
    trading_days,
)
from pi.store import db
from tests.helpers import bhav_rows

MON, TUE, WED, THU, FRI, SAT = (date(2026, 11, d) for d in (16, 17, 18, 19, 20, 21))
NEXT_MON = date(2026, 11, 23)

A_OLD, A_NEW, B = "INE000A01011", "INE000A01029", "INE000B01011"


# --------------------------------------------------------------------------- calendar


def test_expected_days_skip_weekends_and_holidays() -> None:
    assert expected_trading_days(MON, NEXT_MON, holidays={WED}) == [MON, TUE, THU, FRI, NEXT_MON]


def test_a_special_saturday_session_counts_when_its_file_exists() -> None:
    assert trading_days(FRI, NEXT_MON, holidays=(), present={FRI, SAT}) == [FRI, SAT, NEXT_MON]


def test_missing_days() -> None:
    assert missing_days(MON, FRI, holidays={WED}, present={MON, TUE, FRI}) == [THU]


def test_streak_counts_back_to_the_first_gap() -> None:
    ok = {MON, TUE, THU, FRI}
    assert clean_streak(FRI, ok, holidays=(), present=ok) == 2  # WED breaks it


def test_declaring_a_forgotten_holiday_repairs_the_streak() -> None:
    ok = {MON, TUE, THU, FRI}
    assert clean_streak(FRI, ok, holidays={WED}, present=ok) == 4


def test_streak_is_zero_if_the_last_day_is_not_clean() -> None:
    assert clean_streak(FRI, {MON, TUE}, holidays=(), present=()) == 0


def test_duplicate_holiday_is_rejected() -> None:
    h = {"date": "2026-12-25", "name": "Christmas", "source": "test"}
    with pytest.raises(ValidationError, match="twice"):
        CalendarConfig.model_validate({"holidays": [h, h]})


def test_holiday_yaml_key_is_date() -> None:
    cal = CalendarConfig.model_validate(
        {"holidays": [{"date": "2026-12-25", "name": "Christmas", "source": "test"}]}
    )
    assert cal.holiday_dates == frozenset({date(2026, 12, 25)})


# --------------------------------------------------------------------------- securities


@pytest.fixture
def con(tmp_path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    c = db.connect(tmp_path / "pi.duckdb")
    db.ensure_schema(c, db.CORE_DDL)
    db.ensure_schema(c, sec.DDL)
    yield c
    c.close()


def sync(con, d: date, rows):
    return sec.sync_day(con, d, bhav_rows(d, rows=rows))


def test_new_isin_gets_a_new_id_and_keeps_it(con) -> None:
    r1 = sync(con, MON, [(A_OLD, "AAA", "EQ"), (B, "BBB", "EQ")])
    assert len(r1.new_securities) == 2 and r1.provisional == []
    a = sec.security_for_isin(con, A_OLD)
    sync(con, TUE, [(A_OLD, "AAA", "EQ"), (B, "BBB", "EQ")])
    assert sec.security_for_isin(con, A_OLD) == a
    assert con.execute("SELECT count(*) FROM securities").fetchone()[0] == 2


def test_reapplying_a_day_is_a_no_op(con) -> None:
    sync(con, MON, [(A_OLD, "AAA", "EQ")])
    before = con.execute("SELECT * FROM symbol_history ORDER BY ALL").fetchall()
    assert sync(con, MON, [(A_OLD, "ZZZ", "BE")]).skipped
    assert con.execute("SELECT * FROM symbol_history ORDER BY ALL").fetchall() == before


def test_symbol_change_keeps_the_id_and_records_history(con) -> None:
    sync(con, MON, [(A_OLD, "AAA", "EQ")])
    r = sync(con, TUE, [(A_OLD, "AAANEW", "EQ")])
    a = sec.security_for_isin(con, A_OLD)
    assert r.symbol_changes == 1
    assert sec.symbol_on(con, a, MON) == "AAA"
    assert sec.symbol_on(con, a, TUE) == "AAANEW"


def test_move_to_trade_for_trade_is_not_a_new_security(con) -> None:
    sync(con, MON, [(A_OLD, "AAA", "EQ")])
    r = sync(con, TUE, [(A_OLD, "AAA", "BE")])
    a = sec.security_for_isin(con, A_OLD)
    assert r.new_securities == [] and r.series_changes == 1
    assert (sec.series_on(con, a, MON), sec.series_on(con, a, TUE)) == ("EQ", "BE")


def test_non_equity_series_get_no_id(con) -> None:
    r = sync(con, MON, [(A_OLD, "AAA", "EQ"), ("IN0020200000", "SGBX", "GB")])
    assert len(r.new_securities) == 1
    assert sec.security_for_isin(con, "IN0020200000") is None


def test_one_isin_in_two_series_on_one_day_takes_the_priority_series(con) -> None:
    sync(con, MON, [(A_OLD, "AAA", "BE"), (A_OLD, "AAA", "EQ")])
    a = sec.security_for_isin(con, A_OLD)
    assert sec.series_on(con, a, MON) == "EQ"
    assert con.execute("SELECT count(*) FROM securities").fetchone()[0] == 1


def test_split_without_a_link_is_not_guessed(con) -> None:
    sync(con, MON, [(A_OLD, "AAA", "EQ")])
    r = sync(con, TUE, [(A_NEW, "AAA", "EQ")])  # ex-date: new ISIN, same symbol
    assert len(r.provisional) == 1
    issues = sec.open_issues(con)
    assert [(i[0], i[2]) for i in issues] == [("new_isin_for_existing_symbol", A_NEW)]


def test_linking_merges_the_provisional_id_into_the_old_one(con) -> None:
    sync(con, MON, [(A_OLD, "AAA", "EQ")])
    old = sec.security_for_isin(con, A_OLD)
    sync(con, TUE, [(A_NEW, "AAA", "EQ")])
    provisional = con.execute(
        "SELECT security_id FROM isin_map WHERE isin = ?", [A_NEW]
    ).fetchone()[0]

    survivor = sec.link_isin(con, A_OLD, A_NEW, TUE, "split 1:5")
    assert survivor == old
    assert sec.security_for_isin(con, A_NEW) == old
    assert sec.canonical(con, provisional) == old  # the retired ID still resolves
    assert (
        con.execute(
            "SELECT count(*) FROM securities WHERE security_id = ?", [provisional]
        ).fetchone()[0]
        == 1
    )  # never deleted
    assert sec.open_issues(con) == []
    assert sec.symbol_on(con, provisional, WED) == "AAA"
    # History is one continuous row, not two overlapping ones.
    rows = con.execute(
        "SELECT symbol, valid_from, valid_to FROM symbol_history WHERE security_id = ?", [old]
    ).fetchall()
    assert rows == [("AAA", MON, None)]


def test_a_link_recorded_before_the_ex_date_is_used_on_arrival(con) -> None:
    sync(con, MON, [(A_OLD, "AAA", "EQ")])
    old = sec.security_for_isin(con, A_OLD)
    sec.link_isin(con, A_OLD, A_NEW, TUE, "split 1:5")  # e.g. from the CA file (WP 1.6)
    r = sync(con, TUE, [(A_NEW, "AAA", "EQ")])
    assert r.new_securities == [] and r.provisional == []
    assert sec.security_for_isin(con, A_NEW) == old
    assert sec.open_issues(con) == []


def test_linking_an_unknown_old_isin_fails(con) -> None:
    with pytest.raises(sec.RefdataError, match="never been seen"):
        sec.link_isin(con, "INE999Z01019", A_NEW, TUE, "split")


def test_conflicting_link_fails(con) -> None:
    sync(con, MON, [(A_OLD, "AAA", "EQ"), (B, "BBB", "EQ")])
    sec.link_isin(con, A_OLD, A_NEW, TUE, "split")
    with pytest.raises(sec.RefdataError, match="already linked"):
        sec.link_isin(con, B, A_NEW, TUE, "split")
