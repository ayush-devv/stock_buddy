"""WP 2.6: dashboard queries, the Streamlit app itself, and the localhost-only launcher."""

from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path

import duckdb
import pytest
from streamlit.testing.v1 import AppTest

from pi import pipeline
from pi.alerts import engine, run_alerts
from pi.alerts import rules as r
from pi.alerts.telegram import AlertMessage
from pi.cli import ui_command
from pi.config import load_all
from pi.store import db
from pi.ui import queries as q
from tests.test_pipeline import drop, ist

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "src" / "pi" / "ui" / "app.py"
THU, FRI = date(2026, 11, 19), date(2026, 11, 20)


class Recorder:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, m: AlertMessage) -> None:
        self.sent.append(m.text())


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A temp project with two processed days and one live and one shadow alert."""
    shutil.copytree(ROOT / "config", tmp_path / "config")
    drop(tmp_path, THU)
    drop(tmp_path, FRI)
    pipeline.run(tmp_path, now=ist(FRI, 19))
    con = db.connect(tmp_path / "data" / "pi.duckdb")
    cfg = load_all(tmp_path / "config").rules.alerts
    dd = r.Condition("DRAWDOWN", "sec:1", "sec:1", "AAA", 1, {"drawdown": -0.2})
    head = r.Condition("LTCG_HEADROOM", "fy:2026", "fy:2026", None, None, {"used": 100000})
    engine.process(
        con,
        [
            r.RuleResult("DRAWDOWN", frozenset({"sec:1"}), (dd,)),
            r.RuleResult("LTCG_HEADROOM", frozenset({"fy:2026"}), (head,)),
        ],
        cfg,
        ist(FRI, 19),
        Recorder(),
        "test",
    )
    con.execute("CHECKPOINT")
    con.close()
    db.export_snapshot(tmp_path / "data" / "pi.duckdb", tmp_path / "data" / "pi_read.duckdb")
    return tmp_path


# --------------------------------------------------------------------------- queries


def test_queries_read_the_snapshot(project: Path) -> None:
    con = q.open_snapshot(project / "data" / "pi_read.duckdb")
    assert con is not None
    s = q.status(con)
    assert (s.last_run_status, s.complete_through, s.streak) == ("ok", "2026-11-20", 2)
    assert [run["trade_date"] for run in q.recent_runs(con)][:2] == [FRI, THU]
    live = q.alerts(con, mode="live")
    assert [(a["rule"], a["subject"], a["delivered"]) for a in live] == [("DRAWDOWN", "AAA", True)]
    assert live[0]["details"] == {"drawdown": -0.2}
    assert q.shadow_counts(con) == {"LTCG_HEADROOM": 1}
    assert q.refdata_issues(con) == []
    con.close()


def test_the_snapshot_connection_cannot_write(project: Path) -> None:
    con = q.open_snapshot(project / "data" / "pi_read.duckdb")
    with pytest.raises(duckdb.Error):
        con.execute("DELETE FROM run_log")
    con.close()


def test_missing_snapshot_and_missing_tables_are_handled(tmp_path: Path) -> None:
    assert q.open_snapshot(tmp_path / "nope.duckdb") is None
    path = tmp_path / "core_only.duckdb"
    con = db.connect(path)
    db.migrate(con, "core", db.CORE_MIGRATIONS)
    con.close()
    ro = db.connect(path, read_only=True)
    assert q.alerts(ro) == [] and q.shadow_counts(ro) == {} and q.refdata_issues(ro) == []
    assert q.status(ro).open_refdata_issues == 0
    ro.close()


def test_ui_code_never_names_the_writable_database() -> None:
    for path in (ROOT / "src" / "pi" / "ui").rglob("*.py"):
        assert '"pi.duckdb"' not in path.read_text(), path


# --------------------------------------------------------------------------- the app


def test_app_renders_the_snapshot(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PI_HOME", str(project))
    at = AppTest.from_file(str(APP), default_timeout=60).run()
    assert not at.exception
    assert at.title[0].value == "Portfolio intelligence"
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Last run"] == "ok"
    assert metrics["Clean streak"] == "2 / 60"
    assert any("DRAWDOWN" in e.label for e in at.expander)


def test_app_without_data_explains_what_to_do(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PI_HOME", str(tmp_path))
    at = AppTest.from_file(str(APP), default_timeout=60).run()
    assert not at.exception
    assert "pi run" in at.info[0].value


def test_launcher_binds_localhost_and_disables_usage_stats() -> None:
    argv = ui_command(8600)
    assert argv[argv.index("--server.address") + 1] == "127.0.0.1"
    assert argv[argv.index("--browser.gatherUsageStats") + 1] == "false"
    assert argv[argv.index("--server.port") + 1] == "8600"
    assert argv[4].endswith("pi/ui/app.py")


def test_run_alerts_also_show_up(project: Path) -> None:
    con = db.connect(project / "data" / "pi.duckdb")
    run_alerts.fire(con, AlertMessage("red", "DATA_MISSING", "2026-11-23"), None, ist(FRI, 20))
    con.execute("CHECKPOINT")
    con.close()
    db.export_snapshot(project / "data" / "pi.duckdb", project / "data" / "pi_read.duckdb")
    ro = q.open_snapshot(project / "data" / "pi_read.duckdb")
    rules = [a["rule"] for a in q.alerts(ro, mode="live")]
    assert "DATA_MISSING" in rules
    ro.close()
