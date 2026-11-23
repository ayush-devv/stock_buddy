"""WP 0.4: CLI stubs."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from pi.cli import app

runner = CliRunner()


def test_status_reports_config_versions(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PI_HOME", str(root))
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    for name in ("rules", "costs", "tax", "restricted", "gate_spec"):
        assert name in result.output
    assert "NOT_EVALUATED" in result.output
    assert "draft: not registered" in result.output


def test_status_fails_on_invalid_config(config_copy: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (config_copy / "costs.yaml").write_text("broker: groww\nschedules: []\n")
    monkeypatch.setenv("PI_HOME", str(config_copy.parent))
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 1
    assert "INVALID" in result.output


def test_run_stub_never_reports_success() -> None:
    result = runner.invoke(app, ["run", "--date", "2026-11-23"])
    assert result.exit_code == 2
    assert "not implemented" in result.output


def test_run_rejects_a_bad_date() -> None:
    result = runner.invoke(app, ["run", "--date", "23-11-2026"])
    assert result.exit_code != 0
    assert "YYYY-MM-DD" in result.output
