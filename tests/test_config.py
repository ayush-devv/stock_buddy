"""WP 0.3: config loading, versioning and dated lookups."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from pi.config import (
    CONFIG_FILES,
    ConfigError,
    CostsConfig,
    GateCriteria,
    RestrictedEntry,
    TaxConfig,
    load_all,
    load_one,
)


def test_real_config_loads(root: Path) -> None:
    cfg = load_all(root / "config")
    assert set(cfg.versions) == set(CONFIG_FILES)
    assert all(re.fullmatch(r"[0-9a-f]{12}", v) for v in cfg.versions.values())
    assert cfg.restricted.entries == []  # A8 confirmed 2026-11-23
    assert not cfg.gate_spec.is_registered  # draft until the user freezes it


@pytest.mark.parametrize("name", CONFIG_FILES)
def test_any_edit_changes_the_version(config_copy: Path, name: str) -> None:
    before = load_one(name, config_copy).version
    path = config_copy / f"{name}.yaml"
    path.write_text(path.read_text() + "\n# touched\n")
    assert load_one(name, config_copy).version != before


def test_missing_file_fails(config_copy: Path) -> None:
    (config_copy / "rules.yaml").unlink()
    with pytest.raises(ConfigError, match="missing"):
        load_one("rules", config_copy)


def test_malformed_yaml_fails(config_copy: Path) -> None:
    (config_copy / "rules.yaml").write_text("decide: [unclosed\n")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_one("rules", config_copy)


def test_unknown_key_fails(config_copy: Path) -> None:
    path = config_copy / "rules.yaml"
    path.write_text(path.read_text().replace("cooldown_days", "cooldown_dayz"))
    with pytest.raises(ConfigError):
        load_one("rules", config_copy)


def test_top_level_must_be_mapping(config_copy: Path) -> None:
    (config_copy / "tax.yaml").write_text("- just a list\n")
    with pytest.raises(ConfigError, match="mapping"):
        load_one("tax", config_copy)


# ---------------------------------------------------------------- dated lookups: tax


@pytest.mark.parametrize(
    ("rule_id", "on", "expected"),
    [
        ("stcg_rate", date(2024, 7, 22), 0.15),
        ("stcg_rate", date(2024, 7, 23), 0.20),
        ("ltcg_rate", date(2024, 7, 23), 0.125),
        ("ltcg_exemption_inr", date(2024, 7, 22), 100000),
        ("ltcg_exemption_inr", date(2026, 11, 23), 125000),
        ("buyback_regime", date(2024, 9, 30), "capital_gains"),
        ("buyback_regime", date(2024, 10, 1), "deemed_dividend"),
        ("buyback_regime", date(2026, 3, 31), "deemed_dividend"),
        ("buyback_regime", date(2026, 4, 1), "capital_gains"),
        ("dividend_tds_threshold_inr", date(2025, 4, 1), 10000),
        ("stcg_87a_rebate", date(2026, 11, 23), False),
    ],
)
def test_tax_lookup_picks_the_entry_in_force(
    root: Path, rule_id: str, on: date, expected: object
) -> None:
    tax = load_all(root / "config").tax
    assert tax.value(rule_id, on) == expected


def test_tax_lookup_outside_coverage_fails(root: Path) -> None:
    tax = load_all(root / "config").tax
    with pytest.raises(ConfigError, match="no entry covers"):
        tax.value("stcg_rate", date(2000, 1, 1))


def test_unknown_tax_rule_fails(root: Path) -> None:
    tax = load_all(root / "config").tax
    with pytest.raises(ConfigError, match="unknown rule"):
        tax.value("no_such_rule", date(2026, 1, 1))


def _tax_rule(value: float, start: str, end: str | None) -> dict[str, object]:
    return {
        "id": "stcg_rate",
        "value": value,
        "effective_from": start,
        "effective_to": end,
        "confidence": "verified",
        "source": "test",
    }


def test_overlapping_tax_entries_are_rejected() -> None:
    rules = [_tax_rule(0.15, "2020-01-01", "2024-07-23"), _tax_rule(0.20, "2024-07-23", None)]
    with pytest.raises(ValidationError, match="overlap"):
        TaxConfig.model_validate({"rules": rules})


def test_open_ended_entry_before_another_is_rejected() -> None:
    rules = [_tax_rule(0.15, "2020-01-01", None), _tax_rule(0.20, "2024-07-23", None)]
    with pytest.raises(ValidationError, match="overlap"):
        TaxConfig.model_validate({"rules": rules})


def test_gap_between_entries_fails_at_lookup() -> None:
    rules = [_tax_rule(0.15, "2020-01-01", "2021-12-31"), _tax_rule(0.20, "2023-01-01", None)]
    tax = TaxConfig.model_validate({"rules": rules})
    with pytest.raises(ConfigError, match="no entry covers 2022-06-01"):
        tax.value("stcg_rate", date(2022, 6, 1))


def test_end_before_start_is_rejected() -> None:
    with pytest.raises(ValidationError, match="before"):
        TaxConfig.model_validate({"rules": [_tax_rule(0.2, "2024-01-01", "2023-01-01")]})


# ---------------------------------------------------------------- dated lookups: costs


def test_costs_lookup(root: Path) -> None:
    costs = load_all(root / "config").costs
    sched = costs.on(date(2026, 11, 23))
    assert costs.broker == "groww"
    assert sched.dp_charge_inr == 20.0
    assert sched.brokerage_min_inr == 5.0
    assert sched.verified is False  # until WP 1.9
    with pytest.raises(ConfigError):
        costs.on(date(2025, 1, 1))  # before the first Groww schedule


def test_overlapping_cost_schedules_are_rejected(root: Path) -> None:
    base = load_all(root / "config").costs.schedules[0].model_dump()
    second = {**base, "effective_from": date(2026, 1, 1)}
    with pytest.raises(ValidationError, match="overlap"):
        CostsConfig.model_validate({"broker": "groww", "schedules": [base, second]})


# ---------------------------------------------------------------- other schemas


def test_contra_period_below_six_months_is_rejected() -> None:
    with pytest.raises(ValidationError):
        RestrictedEntry.model_validate(
            {
                "security_id": "S1",
                "basis": "designated_person",
                "window": "quarter_end_to_results_plus_48h",
                "contra_trade_months": 3,
            }
        )


def test_gate_accepts_an_explicit_auc_significance_bound(root: Path) -> None:
    criteria = load_all(root / "config").gate_spec.criteria.model_dump()
    criteria["auc_ci_lower_gt"] = 0.5
    assert GateCriteria.model_validate(criteria).auc_ci_lower_gt == 0.5
    criteria["auc_ci_lower_gt"] = 0.4
    with pytest.raises(ValidationError):
        GateCriteria.model_validate(criteria)


def test_gate_alarm_must_sit_above_auc_floor(root: Path) -> None:
    criteria = load_all(root / "config").gate_spec.criteria.model_dump()
    criteria["auc_leakage_alarm"] = 0.5
    with pytest.raises(ValidationError, match="auc_leakage_alarm"):
        GateCriteria.model_validate(criteria)
