"""WP 1.4: the evening run (ordering, idempotency, pauses, corrections, snapshot)."""

from __future__ import annotations

import shutil
from datetime import date, datetime
from pathlib import Path

import pytest

from pi import pipeline
from pi.refdata import securities
from pi.store import db
from pi.store.clock import IST
from pi.store.raw import RawStore
from tests.helpers import bhav_rows, sha_of

ROOT = Path(__file__).resolve().parents[1]
MON, TUE, WED, THU, FRI = (date(2026, 11, d) for d in (16, 17, 18, 19, 20))


def ist(d: date, hh: int, mm: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=IST)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    shutil.copytree(ROOT / "config", tmp_path / "config")
    return tmp_path


def drop(project: Path, d: date, at: datetime | None = None, close: float = 100.0) -> None:
    frame = bhav_rows(d, rows=[("INE000A01011", "AAA", "EQ")], close=close)
    RawStore(project / "data" / "raw").write(
        "bhavcopy", d, frame, source_file_sha=sha_of(frame), ingested_at=at or ist(d, 18, 30)
    )


def state(project: Path) -> dict[str, str]:
    con = db.connect(project / "data" / "pi_read.duckdb", read_only=True)
    try:
        return dict(con.execute("SELECT key, value FROM system_state").fetchall())
    finally:
        con.close()


def statuses(project: Path) -> list[tuple]:
    con = db.connect(project / "data" / "pi_read.duckdb", read_only=True)
    try:
        return con.execute(
            "SELECT trade_date, status FROM run_log ORDER BY finished_at, trade_date"
        ).fetchall()
    finally:
        con.close()


def add_holiday(project: Path, d: date) -> None:
    path = project / "config" / "calendar.yaml"
    path.write_text(
        path.read_text().replace(
            "holidays: []", f"holidays:\n  - {{date: {d.isoformat()}, name: test, source: test}}"
        )
    )


def test_no_data_pauses_and_still_writes_a_snapshot(project: Path) -> None:
    result = pipeline.run(project, now=ist(FRI, 19))
    assert result.status == "no_data"
    assert result.exit_code == pipeline.EXIT_PAUSED
    assert (project / "data" / "pi_read.duckdb").exists()
    for sub in ("inbox", "processed", "quarantine", "raw"):
        assert (project / "data" / sub).is_dir()


def test_processes_pending_days_in_order(project: Path) -> None:
    for d in (WED, THU, FRI):
        drop(project, d)
    result = pipeline.run(project, now=ist(FRI, 19))
    assert result.status == "ok" and result.exit_code == 0
    assert result.processed == [WED, THU, FRI]
    assert result.streak == 3
    s = state(project)
    assert s[pipeline.LAST_COMPLETE] == FRI.isoformat()
    assert s[securities.STATE_KEY] == FRI.isoformat()


def test_running_again_changes_nothing(project: Path) -> None:
    for d in (WED, THU, FRI):
        drop(project, d)
    pipeline.run(project, now=ist(FRI, 19))
    con = db.connect(project / "data" / "pi_read.duckdb", read_only=True)
    before = con.execute("SELECT * FROM symbol_history ORDER BY ALL").fetchall()
    con.close()

    result = pipeline.run(project, now=ist(FRI, 21))
    assert result.status == "noop" and result.exit_code == 0
    con = db.connect(project / "data" / "pi_read.duckdb", read_only=True)
    assert con.execute("SELECT * FROM symbol_history ORDER BY ALL").fetchall() == before
    assert con.execute("SELECT count(*) FROM securities").fetchone()[0] == 1
    con.close()


def test_todays_file_waits_until_1800_ist(project: Path) -> None:
    drop(project, THU)
    drop(project, FRI, at=ist(FRI, 17))  # dropped early
    result = pipeline.run(project, now=ist(FRI, 17, 30))
    assert result.processed == [THU]


def test_missing_day_pauses_and_later_days_wait(project: Path) -> None:
    for d in (MON, TUE, THU):
        drop(project, d)
    result = pipeline.run(project, now=ist(THU, 20))
    assert result.status == "paused_missing"
    assert result.exit_code == pipeline.EXIT_PAUSED
    assert result.processed == [MON, TUE]
    assert result.missing == [WED]
    assert "calendar.yaml" in result.message and "data/inbox" in result.message
    assert state(project)[pipeline.LAST_COMPLETE] == TUE.isoformat()


def test_dropping_the_missing_file_resumes(project: Path) -> None:
    for d in (MON, TUE, THU):
        drop(project, d)
    pipeline.run(project, now=ist(THU, 20))
    drop(project, WED, at=ist(FRI, 9))
    result = pipeline.run(project, now=ist(FRI, 10))
    assert result.processed == [WED, THU]
    assert result.streak == 4


def test_declaring_a_holiday_resumes_and_repairs_the_streak(project: Path) -> None:
    for d in (MON, TUE, THU):
        drop(project, d)
    pipeline.run(project, now=ist(THU, 20))
    add_holiday(project, WED)
    result = pipeline.run(project, now=ist(THU, 21))
    assert result.processed == [THU]
    assert result.streak == 3  # MON, TUE, THU; WED is a holiday, not a gap


def test_correction_after_completion_is_flagged_until_reprocessed(project: Path) -> None:
    drop(project, THU, close=100.0)
    pipeline.run(project, now=ist(THU, 19))
    drop(project, THU, at=ist(FRI, 9), close=101.0)  # NSE re-issues THU's file

    pipeline.run(project, now=ist(FRI, 10))
    assert state(project)["corrections_pending"] == f'["{THU.isoformat()}"]'

    result = pipeline.run(project, now=ist(FRI, 11), force_date=THU)
    assert result.status == "ok"
    assert state(project)["corrections_pending"] == "[]"


def test_forcing_a_new_date_cannot_jump_over_missing_days(project: Path) -> None:
    drop(project, MON)
    pipeline.run(project, now=ist(MON, 19))
    drop(project, THU)  # TUE and WED have no files
    result = pipeline.run(project, now=ist(THU, 19), force_date=THU)
    assert result.status == "refused" and result.exit_code == pipeline.EXIT_FAILED
    assert state(project)[pipeline.LAST_COMPLETE] == MON.isoformat()
    # The normal run still stops at the first gap.
    assert pipeline.run(project, now=ist(THU, 20)).missing == [TUE, WED]


def test_forcing_before_anything_is_complete_is_refused(project: Path) -> None:
    drop(project, FRI)
    result = pipeline.run(project, now=ist(FRI, 17), force_date=FRI)  # also before 18:00
    assert result.status == "refused"


def test_forcing_a_completed_date_whose_file_is_gone_pauses(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    drop(project, THU)
    pipeline.run(project, now=ist(THU, 19))
    monkeypatch.setattr(RawStore, "partitions", lambda self, source: [])
    result = pipeline.run(project, now=ist(FRI, 10), force_date=THU)
    assert result.status == "paused_missing" and result.missing == [THU]


def test_a_failing_day_rolls_back_and_exits_1(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    drop(project, THU)

    def boom(*args, **kwargs):
        raise RuntimeError("parser bug")

    monkeypatch.setattr(securities, "sync_day", boom)
    result = pipeline.run(project, now=ist(THU, 19))
    assert result.status == "failed" and result.exit_code == pipeline.EXIT_FAILED
    assert "parser bug" in result.message
    assert pipeline.LAST_COMPLETE not in state(project)
    assert (THU, "failed") in statuses(project)


def test_a_second_concurrent_run_is_refused(project: Path) -> None:
    (project / "data").mkdir()
    with pipeline.run_lock(project / "data" / ".run.lock"), pytest.raises(pipeline.RunLocked):
        pipeline.run(project, now=ist(FRI, 19))


def test_an_open_snapshot_reader_does_not_block_the_run(project: Path) -> None:
    drop(project, THU)
    pipeline.run(project, now=ist(THU, 19))
    reader = db.connect(project / "data" / "pi_read.duckdb", read_only=True)
    try:
        drop(project, FRI)
        assert pipeline.run(project, now=ist(FRI, 19)).processed == [FRI]
    finally:
        reader.close()
