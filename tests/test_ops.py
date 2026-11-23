"""WP 1.5: Keychain secrets, heartbeat, Telegram content rules, run alerts, launchd."""

from __future__ import annotations

import json
import plistlib
import shutil
import subprocess
import urllib.error
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pi import pipeline
from pi.alerts import run_alerts
from pi.alerts.telegram import AlertContentError, AlertMessage, TelegramNotifier
from pi.cli import app
from pi.ops import heartbeat, launchd, secrets
from pi.store import db
from pi.store.clock import IST

ROOT = Path(__file__).resolve().parents[1]
runner = CliRunner()
TOKEN = "123456:SECRET-TOKEN"


# --------------------------------------------------------------------------- secrets


def test_secrets_round_trip_without_exposing_values() -> None:
    assert secrets.status() == {name: False for name in secrets.KNOWN}
    secrets.put("telegram_chat_id", "  42  ")
    assert secrets.get("telegram_chat_id") == "42"
    assert secrets.status()["telegram_chat_id"] is True
    secrets.delete("telegram_chat_id")
    secrets.delete("telegram_chat_id")  # deleting twice is fine
    assert secrets.get("telegram_chat_id") is None


@pytest.mark.parametrize(("name", "value"), [("nope", "x"), ("heartbeat_url", "   ")])
def test_bad_secrets_are_refused(name: str, value: str) -> None:
    with pytest.raises(secrets.SecretError):
        secrets.put(name, value)


# --------------------------------------------------------------------------- heartbeat


def test_ping_sends_one_bare_get() -> None:
    seen: list[str] = []
    result = heartbeat.ping("https://hc.example/ping/abc", get=lambda u: seen.append(u) or 200)
    assert result.ok and seen == ["https://hc.example/ping/abc"]


@pytest.mark.parametrize(
    ("url", "detail"),
    [
        (None, "not configured"),
        ("http://hc.example/ping/abc", "https"),
        ("https://hc.example/ping/abc?holdings=5", "query"),
    ],
)
def test_ping_refuses_bad_or_missing_urls(url: str | None, detail: str) -> None:
    result = heartbeat.ping(url, get=lambda u: 200)
    assert not result.ok and detail in result.detail


def test_ping_failures_never_raise() -> None:
    def down(url: str) -> int:
        raise urllib.error.URLError("no route")

    assert not heartbeat.ping("https://hc.example/p", get=down).ok
    assert not heartbeat.ping("https://hc.example/p", get=lambda u: 503).ok


# --------------------------------------------------------------------------- telegram


@pytest.mark.parametrize(
    ("subject", "rule"),
    [
        ("Rs 5000", "DRAWDOWN"),  # amounts
        ("RELIANCE 10 shares", "DRAWDOWN"),  # holdings
        ("reliance", "DRAWDOWN"),  # prose / lower case
        ("AAA", "drawdown hit"),  # rule must be UPPER_SNAKE
    ],
)
def test_messages_can_carry_only_severity_rule_and_subject(subject: str, rule: str) -> None:
    with pytest.raises(AlertContentError):
        AlertMessage("red", rule, subject)


@pytest.mark.parametrize(
    ("msg", "text"),
    [
        (AlertMessage("red", "DATA_MISSING", "2026-11-18"), "🔴 DATA_MISSING 2026-11-18"),
        (AlertMessage("yellow", "RESULTS_AHEAD", "M&M"), "🟡 RESULTS_AHEAD M&M"),
        (AlertMessage("red", "NO_DATA"), "🔴 NO_DATA"),
    ],
)
def test_message_text(msg: AlertMessage, text: str) -> None:
    assert msg.text() == text


def test_notifier_posts_only_chat_id_and_text() -> None:
    sent: list[tuple[str, dict]] = []
    n = TelegramNotifier(TOKEN, "42", post=lambda u, b: sent.append((u, json.loads(b))) or 200)
    n.send(AlertMessage("red", "NO_DATA"))
    assert sent == [
        (
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            {"chat_id": "42", "text": "🔴 NO_DATA"},
        )
    ]


def test_send_errors_never_leak_the_token() -> None:
    def fail(url: str, body: bytes) -> int:
        raise urllib.error.URLError(f"refused {url}")

    with pytest.raises(RuntimeError) as info:
        TelegramNotifier(TOKEN, "42", post=fail).send(AlertMessage("red", "NO_DATA"))
    assert TOKEN not in str(info.value)
    assert info.value.__cause__ is None and info.value.__suppress_context__


# --------------------------------------------------------------------------- run alerts


class FakeNotifier:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[str] = []
        self.fail = fail

    def send(self, message: AlertMessage) -> None:
        if self.fail:
            raise RuntimeError("telegram answered 500")
        self.sent.append(message.text())


@pytest.fixture
def con(tmp_path: Path):
    c = db.connect(tmp_path / "pi.duckdb")
    db.migrate(c, "alerts", run_alerts.MIGRATIONS)
    yield c
    c.close()


T0 = datetime(2026, 11, 20, 19, tzinfo=IST)
MSG = AlertMessage("red", "DATA_MISSING", "2026-11-18")


def test_alert_is_delivered_and_recorded(con) -> None:
    n = FakeNotifier()
    assert run_alerts.fire(con, MSG, n, T0) == "delivered"
    assert n.sent == ["🔴 DATA_MISSING 2026-11-18"]
    assert con.execute("SELECT delivered, mode FROM alerts").fetchall() == [(True, "live")]


def test_same_alert_is_not_resent_within_cooldown(con) -> None:
    n = FakeNotifier()
    run_alerts.fire(con, MSG, n, T0)
    assert run_alerts.fire(con, MSG, n, T0 + timedelta(hours=3)) == "cooldown"
    assert run_alerts.fire(con, MSG, n, T0 + timedelta(hours=21)) == "delivered"
    assert len(n.sent) == 2


def test_undeliverable_alerts_are_still_recorded(con) -> None:
    assert "not configured" in run_alerts.fire(con, MSG, None, T0)
    assert "500" in run_alerts.fire(con, MSG, FakeNotifier(fail=True), T0)
    rows = con.execute("SELECT delivered, error FROM alerts ORDER BY error").fetchall()
    assert rows == [(False, "telegram answered 500"), (False, "telegram not configured")]


def test_paused_run_fires_data_missing_with_the_date(tmp_path: Path) -> None:
    shutil.copytree(ROOT / "config", tmp_path / "config")
    from tests.test_pipeline import drop, ist

    mon, wed = date(2026, 11, 16), date(2026, 11, 18)
    drop(tmp_path, mon)
    drop(tmp_path, wed)
    n = FakeNotifier()
    result = pipeline.run(tmp_path, now=ist(wed, 19), notifier=n)
    assert result.status == "paused_missing"
    assert n.sent == ["🔴 DATA_MISSING 2026-11-17"]

    n2 = FakeNotifier()
    drop(tmp_path, date(2026, 11, 17))
    assert pipeline.run(tmp_path, now=ist(wed, 20), notifier=n2).status == "ok"
    assert n2.sent == []  # clean runs are silent


# --------------------------------------------------------------------------- launchd


def test_plist_runs_the_venv_pi_at_1900_and_at_login(tmp_path: Path) -> None:
    spec = plistlib.loads(launchd.render(tmp_path))
    assert spec["Label"] == launchd.LABEL
    assert spec["ProgramArguments"] == [str(tmp_path.resolve() / ".venv/bin/pi"), "run"]
    assert spec["StartCalendarInterval"] == {"Hour": 19, "Minute": 0}
    assert spec["RunAtLoad"] is True
    assert spec["EnvironmentVariables"] == {"PI_HOME": str(tmp_path.resolve())}
    assert spec["StandardErrorPath"].startswith(str(tmp_path.resolve() / "data/logs"))


class FakeLaunchctl:
    def __init__(self, bootstrap_rc: int = 0) -> None:
        self.calls: list[list[str]] = []
        self.rc = bootstrap_rc

    def __call__(self, args):
        self.calls.append(list(args))
        rc = self.rc if args[1] == "bootstrap" else 0
        return subprocess.CompletedProcess(args, rc, "", "boom" if rc else "")


def _fake_project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    (project / ".venv" / "bin").mkdir(parents=True)
    (project / ".venv" / "bin" / "pi").write_text("#!/bin/sh\n")
    return project


def test_install_writes_the_plist_and_bootstraps(tmp_path: Path) -> None:
    project, home, fake = _fake_project(tmp_path), tmp_path / "home", FakeLaunchctl()
    path = launchd.install(project, home=home, runner=fake)
    assert path == home / "Library/LaunchAgents" / f"{launchd.LABEL}.plist"
    assert plistlib.loads(path.read_bytes())["Label"] == launchd.LABEL
    assert [c[1] for c in fake.calls] == ["bootout", "bootstrap"]
    assert (project / "data" / "logs").is_dir()

    assert launchd.uninstall(home=home, runner=fake) is True
    assert not path.exists()
    assert launchd.uninstall(home=home, runner=fake) is False


def test_install_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="uv sync"):
        launchd.install(tmp_path / "empty", home=tmp_path / "home", runner=FakeLaunchctl())
    with pytest.raises(RuntimeError, match="boom"):
        launchd.install(
            _fake_project(tmp_path), home=tmp_path / "home", runner=FakeLaunchctl(bootstrap_rc=5)
        )


# --------------------------------------------------------------------------- CLI


def test_cli_secret_status_and_notify_test_without_config() -> None:
    result = runner.invoke(app, ["secret", "status"])
    assert result.exit_code == 0 and "telegram_bot_token" in result.output
    assert "missing" in result.output
    result = runner.invoke(app, ["notify-test"])
    assert result.exit_code == 1 and "not configured" in result.output


def test_cli_notify_test_sends_through_telegram(monkeypatch: pytest.MonkeyPatch) -> None:
    secrets.put("telegram_bot_token", TOKEN)
    secrets.put("telegram_chat_id", "42")
    sent: list[dict] = []
    monkeypatch.setattr(
        "pi.alerts.telegram._post", lambda url, body: sent.append(json.loads(body)) or 200
    )
    result = runner.invoke(app, ["notify-test"])
    assert result.exit_code == 0, result.output
    assert sent == [{"chat_id": "42", "text": "🟡 TEST"}]


def test_cli_secret_set_validates_heartbeat_url() -> None:
    result = runner.invoke(app, ["secret", "set", "heartbeat_url"], input="http://x.example\n")
    assert result.exit_code != 0
    assert secrets.get("heartbeat_url") is None
    result = runner.invoke(
        app, ["secret", "set", "heartbeat_url"], input="https://hc.example/ping/abc\n"
    )
    assert result.exit_code == 0
    assert secrets.get("heartbeat_url") == "https://hc.example/ping/abc"


def test_cli_run_pings_only_after_a_clean_run(
    config_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PI_HOME", str(config_copy.parent))
    secrets.put("heartbeat_url", "https://hc.example/ping/abc")
    pinged: list[str] = []
    monkeypatch.setattr("pi.ops.heartbeat._get", lambda url: pinged.append(url) or 200)

    result = runner.invoke(app, ["run"])  # no data: paused, exit 3
    assert result.exit_code == 3 and pinged == []
    assert "alert: not delivered: telegram not configured" in result.output


def test_cli_clean_run_pings_once_and_forced_rerun_does_not(
    config_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_pipeline import drop, ist

    project = config_copy.parent
    monkeypatch.setenv("PI_HOME", str(project))
    secrets.put("heartbeat_url", "https://hc.example/ping/abc")
    pinged: list[str] = []
    monkeypatch.setattr("pi.ops.heartbeat._get", lambda url: pinged.append(url) or 200)
    thu = date(2026, 11, 19)
    drop(project, thu)
    monkeypatch.setattr("pi.pipeline.utc_now", lambda: ist(thu, 19).astimezone())

    result = runner.invoke(app, ["run"])
    assert result.exit_code == 0, result.output
    assert pinged == ["https://hc.example/ping/abc"]
    assert "heartbeat: pinged (200)" in result.output

    result = runner.invoke(app, ["run", "--date", thu.isoformat()])
    assert result.exit_code == 0, result.output
    assert pinged == ["https://hc.example/ping/abc"]  # a manual re-run is not a heartbeat
