"""WP 3.5: research harness, trial log, lockbox cutoff and purged walk-forward folds.

Every test works on a temporary copy of config/; the real gate spec is never touched.
"""

from __future__ import annotations

import shutil
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from pi.config import GateSpec, load_one
from pi.refdata.calendar import expected_trading_days
from pi.research import harness, lockbox
from pi.research.loader import ResearchLoader
from pi.research.splits import walk_forward
from pi.store import db
from pi.store.clock import IST
from pi.store.raw import RawStore
from tests.helpers import bhav_rows, sha_of

ROOT = Path(__file__).resolve().parents[1]
SPEC_FILE = "gate_" + "spec.yaml"
LOCK_START = date(2026, 1, 1)
T0 = datetime(2026, 2, 1, 12, tzinfo=UTC)
FAKE_GIT = harness.GitState("abc123", dirty=True, diff_hash="d" * 64)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    shutil.copytree(ROOT / "config", tmp_path / "config")
    return tmp_path


def register(project: Path, extra: str = "") -> None:
    path = project / "config" / SPEC_FILE
    text = path.read_text().replace(
        "registered_at: null", "registered_at: 2026-01-15T10:00:00+05:30"
    )
    path.write_text(text + extra)


def seal(project: Path, start: date = LOCK_START, at: datetime = T0 - timedelta(days=10)) -> None:
    con = harness.open_research_db(project)
    lockbox.record_seal(con, start, "h" * 64, at)
    con.close()


def trial(
    project: Path, evaluate=lambda: {"auc": 0.53}, window=(date(2020, 1, 1), date(2025, 6, 30))
):
    clock = iter(T0 + timedelta(seconds=i) for i in range(100))
    return harness.run_trial(
        project, {"model": "lgbm", "depth": 3}, "fs1", window, evaluate,
        now=lambda: next(clock), git=lambda root: FAKE_GIT,
    )  # fmt: skip


def rows(project: Path) -> list[tuple]:
    con = harness.open_research_db(project)
    try:
        return con.execute(
            "SELECT seq, kind, git_sha, git_dirty FROM trials ORDER BY seq"
        ).fetchall()
    finally:
        con.close()


# --------------------------------------------------------------------------- preflight


def test_nothing_runs_while_the_spec_is_a_draft(project: Path) -> None:
    seal(project)
    with pytest.raises(harness.ResearchNotReady, match="not registered"):
        trial(project)
    assert rows(project) == []


def test_nothing_runs_before_a_lockbox_is_sealed(project: Path) -> None:
    register(project)
    with pytest.raises(harness.ResearchNotReady, match="no unused lockbox"):
        trial(project)


def test_the_real_spec_is_still_a_draft() -> None:
    """Reminder that the harness is inactive until you freeze the spec yourself."""
    assert not load_one("gate_spec", ROOT / "config").model.is_registered


def test_a_data_window_reaching_into_the_lockbox_is_refused(project: Path) -> None:
    register(project)
    seal(project)
    with pytest.raises(harness.ResearchNotReady, match="inside the lockbox"):
        trial(project, window=(date(2020, 1, 1), LOCK_START))


# --------------------------------------------------------------------------- logging


def test_a_trial_is_logged_before_its_metrics_exist(project: Path) -> None:
    register(project)
    seal(project)
    seen = {}

    def evaluate():
        con = db.connect(harness.research_db_path(project))
        seen["kinds"] = [k for (k,) in con.execute("SELECT kind FROM trials").fetchall()]
        con.close()
        return {"auc": 0.53}

    assert trial(project, evaluate) == {"auc": 0.53}
    assert seen["kinds"] == ["started"]  # committed before evaluate ran
    assert rows(project) == [(1, "started", "abc123", True), (2, "finished", "abc123", True)]


def test_failed_trials_still_count(project: Path) -> None:
    register(project)
    seal(project)

    def boom():
        raise RuntimeError("model crashed")

    with pytest.raises(RuntimeError, match="crashed"):
        trial(project, boom)
    trial(project)
    con = harness.open_research_db(project)
    assert harness.trial_count(con) == 2
    assert [k for (_, k, _, _) in rows(project)] == ["started", "failed", "started", "finished"]
    con.close()


def test_config_hash_ignores_key_order() -> None:
    assert harness.canonical_hash({"a": 1, "b": 2}) == harness.canonical_hash({"b": 2, "a": 1})
    assert harness.canonical_hash({"a": 1}) != harness.canonical_hash({"a": 2})


def test_editing_or_deleting_a_trial_row_is_detected_and_blocks_new_trials(project: Path) -> None:
    register(project)
    seal(project)
    trial(project)
    trial(project)
    con = harness.open_research_db(project)
    assert harness.verify_chain(con) == []
    con.execute('UPDATE trials SET detail_json = \'{"metrics": {"auc": 0.9}}\' WHERE seq = 2')
    assert any("edited" in p for p in harness.verify_chain(con))
    con.close()
    with pytest.raises(harness.ResearchNotReady, match="verification"):
        trial(project)


def test_deleting_a_middle_row_breaks_the_chain(project: Path) -> None:
    register(project)
    seal(project)
    trial(project)
    trial(project)
    con = harness.open_research_db(project)
    con.execute("DELETE FROM trials WHERE seq = 2")
    problems = harness.verify_chain(con)
    assert any("missing" in p for p in problems) and any("chain broken" in p for p in problems)
    con.close()


def test_changing_the_spec_after_trials_needs_a_new_lockbox(project: Path) -> None:
    register(project)
    seal(project)
    trial(project)
    (project / "config" / SPEC_FILE).write_text(
        (project / "config" / SPEC_FILE).read_text() + "\n# tweaked\n"
    )
    with pytest.raises(harness.ResearchNotReady, match="changed after trials"):
        trial(project)
    # The gate burns the lockbox (WP 3.7); a new seal allows the new spec.
    con = harness.open_research_db(project)
    con.execute("UPDATE lockbox_seal SET burned_at = ?", [T0])
    lockbox.record_seal(con, date(2026, 6, 1), "n" * 64, T0 + timedelta(seconds=50))
    con.close()
    clock = iter(T0 + timedelta(seconds=60 + i) for i in range(10))
    harness.run_trial(
        project,
        {"m": 1},
        "fs1",
        (date(2020, 1, 1), date(2025, 12, 31)),
        lambda: {"auc": 0.52},
        now=lambda: next(clock),
        git=lambda r: FAKE_GIT,
    )


def test_an_unused_lockbox_cannot_be_replaced(project: Path) -> None:
    seal(project)
    con = harness.open_research_db(project)
    with pytest.raises(RuntimeError, match="already sealed"):
        lockbox.record_seal(con, date(2027, 1, 1), "x" * 64, T0)
    con.close()


# --------------------------------------------------------------------------- folds


def test_walk_forward_hand_worked() -> None:
    """Monthly samples (every 21 trading days), 60-day labels, 20-day embargo.
    The fold testing day 252 may train only on samples with j + 1 + 60 + 20 <= 252,
    i.e. j <= 171: days 0, 21, ..., 168 (nine samples). Days 189-231 are purged."""
    days = list(range(0, 21 * 16, 21))  # 0 .. 315
    folds = walk_forward(days, horizon=60, embargo=20, test_size=3, min_train=5)
    by_start = {days[f.test[0]]: f for f in folds}
    # Test blocks start at 0, 63, 126, 189, 252, 315. The first with >= 5 training samples
    # is 189: j <= 189 - 81 = 108 leaves days 0..105 (six samples).
    assert sorted(by_start) == [189, 252, 315]
    assert [days[i] for i in by_start[189].train] == [0, 21, 42, 63, 84, 105]
    f = by_start[252]
    assert [days[i] for i in f.test] == [252, 273, 294]
    assert [days[i] for i in f.train] == [0, 21, 42, 63, 84, 105, 126, 147, 168]


def test_walk_forward_rejects_bad_input() -> None:
    with pytest.raises(ValueError, match="strictly increasing"):
        walk_forward([0, 21, 21], 60, 20, 1, 1)
    with pytest.raises(ValueError):
        walk_forward([0, 21], 0, 20, 1, 1)


def test_spec_purge_must_cover_the_label_horizon() -> None:
    data = load_one("gate_spec", ROOT / "config").model.model_dump()
    data["walk_forward"]["purge_trading_days"] = 30
    with pytest.raises(ValidationError, match="purge"):
        GateSpec.model_validate(data)


# --------------------------------------------------------------------------- the loader


@pytest.fixture
def loader(project: Path) -> tuple[ResearchLoader, RawStore]:
    store = RawStore(project / "data" / "raw")
    days = expected_trading_days(date(2025, 1, 1), date(2026, 3, 31), ())
    for d in (date(2025, 12, 30), date(2025, 12, 31), date(2026, 1, 2)):
        f = bhav_rows(d)
        store.write(
            "bhavcopy",
            d,
            f,
            source_file_sha=sha_of(f),
            ingested_at=datetime(d.year, d.month, d.day, 19, tzinfo=IST),
        )
    spec = load_one("gate_spec", project / "config").model
    s = lockbox.Seal(LOCK_START, "h" * 64, T0, burned=False)
    return ResearchLoader(store, days, s, spec), store


def test_loader_never_returns_lockbox_rows(loader) -> None:
    ld, _ = loader
    df = ld.frame("bhavcopy", end=date(2026, 3, 1))  # asks past the cutoff
    assert df.get_column("trade_date").max() == date(2025, 12, 31)


def test_corrections_that_arrive_during_the_lockbox_stay_invisible(loader) -> None:
    ld, store = loader
    fix = bhav_rows(date(2025, 12, 31), close=999.0)
    store.write(
        "bhavcopy",
        date(2025, 12, 31),
        fix,
        source_file_sha=sha_of(fix),
        ingested_at=datetime(2026, 1, 20, 10, tzinfo=IST),
    )
    closes = ld.frame("bhavcopy").filter(ld.frame("bhavcopy")["trade_date"] == date(2025, 12, 31))
    assert closes.get_column("close").to_list() == [100.0]


def test_no_sample_label_window_touches_the_lockbox(loader) -> None:
    ld, _ = loader
    days = expected_trading_days(date(2025, 1, 1), date(2026, 3, 31), ())
    lock_idx = days.index(date(2026, 1, 1))
    allowed = ld.sample_days()
    assert all(days.index(d) + 1 + 60 < lock_idx for d in allowed)
    assert days[lock_idx - 62] in allowed and days[lock_idx - 61] not in allowed
    with pytest.raises(ValueError, match="reach the lockbox"):
        ld.folds([days[lock_idx - 61]], test_size=1, min_train=1)
