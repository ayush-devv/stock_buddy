"""Load and validate the versioned YAML files in config/ (WP 0.3).

Each file's version is the first 12 hex characters of the SHA-256 of its bytes, so any
edit, including a comment, produces a new version that later gets stored with every
suggestion and trial.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

CONFIG_FILES = ("rules", "costs", "tax", "restricted", "gate_spec", "calendar")


class ConfigError(Exception):
    """A config file is missing, malformed, or fails validation."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --------------------------------------------------------------------------- dated entries


class _Dated(Protocol):
    effective_from: date
    effective_to: date | None


def _check_no_overlap[D: _Dated](items: list[D], label: str) -> None:
    ordered = sorted(items, key=lambda i: i.effective_from)
    for item in ordered:
        if item.effective_to is not None and item.effective_to < item.effective_from:
            raise ValueError(f"{label}: effective_to before effective_from ({item.effective_from})")
    for prev, nxt in zip(ordered, ordered[1:], strict=False):
        if prev.effective_to is None or prev.effective_to >= nxt.effective_from:
            raise ValueError(
                f"{label}: entries starting {prev.effective_from} and {nxt.effective_from} overlap"
            )


def _select[D: _Dated](items: list[D], on: date, label: str) -> D:
    for item in items:
        if item.effective_from <= on and (item.effective_to is None or on <= item.effective_to):
            return item
    raise ConfigError(f"{label}: no entry covers {on.isoformat()}")


# --------------------------------------------------------------------------- rules.yaml


class DecideRules(_Strict):
    edge_shrinkage: float = Field(gt=0, le=1)
    min_sell_inr: float = Field(gt=0)
    max_trims_per_quarter: int = Field(ge=0)
    cooldown_days: int = Field(ge=0)
    ltcg_defer_days: int = Field(ge=0)


class AlertRules(_Strict):
    shadow_days: int = Field(ge=0)
    urgent_weekly_cap: int = Field(ge=0)
    drawdown_from_peak_pct: float = Field(gt=0, lt=100)
    drawdown_vs_index_pct: float = Field(gt=0, lt=100)
    vol_spike_ratio: float = Field(gt=1)
    ltcg_window_days: int = Field(ge=0)
    ltcg_headroom_warn_pct: float = Field(gt=0, le=100)
    ca_exdate_days: int = Field(ge=0)


class RulesConfig(_Strict):
    decide: DecideRules
    alerts: AlertRules


# --------------------------------------------------------------------------- costs.yaml


class CostSchedule(_Strict):
    effective_from: date
    effective_to: date | None
    verified: bool
    source: str
    brokerage_rate: float = Field(ge=0)
    brokerage_max_inr: float = Field(ge=0)
    brokerage_min_inr: float = Field(ge=0)
    dp_charge_inr: float = Field(ge=0)
    dp_waived_below_inr: float = Field(ge=0)
    gst_rate: float = Field(ge=0, lt=1)
    stt_delivery_rate: float = Field(ge=0, lt=1)
    stt_etf_buy_rate: float = Field(ge=0, lt=1)
    stt_etf_sell_rate: float = Field(ge=0, lt=1)
    exchange_txn_rate: float = Field(ge=0, lt=1)
    sebi_fee_rate: float = Field(ge=0, lt=1)
    stamp_duty_buy_rate: float = Field(ge=0, lt=1)


class CostsConfig(_Strict):
    broker: str
    schedules: list[CostSchedule] = Field(min_length=1)

    @model_validator(mode="after")
    def _no_overlap(self) -> CostsConfig:
        _check_no_overlap(self.schedules, "costs.schedules")
        return self

    def on(self, d: date) -> CostSchedule:
        return _select(self.schedules, d, "costs")


# --------------------------------------------------------------------------- tax.yaml


class TaxRule(_Strict):
    id: str
    value: bool | int | float | str
    effective_from: date
    effective_to: date | None
    confidence: Literal["verified", "secondary", "partly", "unverified"]
    source: str


class TaxConfig(_Strict):
    rules: list[TaxRule] = Field(min_length=1)

    @model_validator(mode="after")
    def _no_overlap(self) -> TaxConfig:
        by_id: dict[str, list[TaxRule]] = {}
        for rule in self.rules:
            by_id.setdefault(rule.id, []).append(rule)
        for rule_id, items in by_id.items():
            _check_no_overlap(items, f"tax.{rule_id}")
        return self

    def rule(self, rule_id: str, on: date) -> TaxRule:
        items = [r for r in self.rules if r.id == rule_id]
        if not items:
            raise ConfigError(f"tax: unknown rule id {rule_id!r}")
        return _select(items, on, f"tax.{rule_id}")

    def value(self, rule_id: str, on: date) -> bool | int | float | str:
        return self.rule(rule_id, on).value


# --------------------------------------------------------------------------- restricted.yaml


class RestrictedEntry(_Strict):
    security_id: str
    basis: Literal["designated_person", "relative_of_dp", "holds_upsi"]
    window: Literal["quarter_end_to_results_plus_48h", "always"]
    contra_trade_months: int = Field(ge=6)
    preclearance_threshold_inr: float | None = None


class RestrictedConfig(_Strict):
    entries: list[RestrictedEntry]


# --------------------------------------------------------------------------- gate_spec.yaml


class GateLabel(_Strict):
    horizon_trading_days: int = Field(gt=0)
    execution: Literal["next_open"]
    benchmark: str
    total_return: bool


class GateWalkForward(_Strict):
    window: Literal["expanding"]
    purge_trading_days: int = Field(ge=0)
    embargo_trading_days: int = Field(ge=0)


class GateCriteria(_Strict):
    brier_skill_ci_lower_gt: float
    ci_level: float = Field(gt=0, lt=1)
    bootstrap: Literal["month_block"]
    auc_gt: float = Field(ge=0.5)
    # "Significantly above 0.5" (§14.1): lower bound of the AUC CI, same bootstrap and level.
    auc_ci_lower_gt: float | None = Field(default=None, ge=0.5)
    auc_leakage_alarm: float
    decile_spread_after_costs_gt: float
    report_deflated_sharpe: bool
    report_pbo: bool

    @model_validator(mode="after")
    def _alarm_above_floor(self) -> GateCriteria:
        if self.auc_leakage_alarm <= self.auc_gt:
            raise ValueError("auc_leakage_alarm must be above auc_gt")
        if self.auc_ci_lower_gt is not None and self.auc_ci_lower_gt >= self.auc_leakage_alarm:
            raise ValueError("auc_ci_lower_gt must be below auc_leakage_alarm")
        return self


class GateLockbox(_Strict):
    years: int = Field(gt=0)
    evaluations_allowed: Literal[1]
    evaluated_by: Literal["user_only"]
    lockbox_brier_skill_gt: float
    lockbox_decile_spread_gt: float


class GateSpec(_Strict):
    spec_version: int = Field(gt=0)
    registered_at: datetime | None
    label: GateLabel
    walk_forward: GateWalkForward
    criteria: GateCriteria
    lockbox: GateLockbox

    @property
    def is_registered(self) -> bool:
        return self.registered_at is not None


# --------------------------------------------------------------------------- calendar.yaml


class Holiday(_Strict):
    day: date = Field(alias="date")  # YAML key is `date`; renamed to avoid shadowing the type
    name: str
    source: str


class CalendarConfig(_Strict):
    holidays: list[Holiday]

    @model_validator(mode="after")
    def _unique(self) -> CalendarConfig:
        seen: set[date] = set()
        for h in self.holidays:
            if h.day in seen:
                raise ValueError(f"holiday {h.day} listed twice")
            seen.add(h.day)
        return self

    @property
    def holiday_dates(self) -> frozenset[date]:
        return frozenset(h.day for h in self.holidays)


# --------------------------------------------------------------------------- loading

_MODELS: dict[str, type[BaseModel]] = {
    "rules": RulesConfig,
    "costs": CostsConfig,
    "tax": TaxConfig,
    "restricted": RestrictedConfig,
    "gate_spec": GateSpec,
    "calendar": CalendarConfig,
}


def project_root() -> Path:
    """`PI_HOME` if set, else the current directory (launchd sets WorkingDirectory)."""
    return Path(os.environ.get("PI_HOME", Path.cwd())).resolve()


def file_version(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()[:12]


@dataclass(frozen=True)
class Loaded:
    name: str
    path: Path
    version: str
    model: Any


def load_one(name: str, config_dir: Path) -> Loaded:
    if name not in _MODELS:
        raise ConfigError(f"unknown config {name!r}")
    path = config_dir / f"{name}.yaml"
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise ConfigError(f"{path} is missing") from exc
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    try:
        model = _MODELS[name].model_validate(data)
    except (ValidationError, ConfigError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    return Loaded(name=name, path=path, version=file_version(raw), model=model)


@dataclass(frozen=True)
class Configs:
    rules: RulesConfig
    costs: CostsConfig
    tax: TaxConfig
    restricted: RestrictedConfig
    gate_spec: GateSpec
    calendar: CalendarConfig
    versions: dict[str, str]


def load_all(config_dir: Path | None = None) -> Configs:
    config_dir = config_dir or project_root() / "config"
    loaded = {name: load_one(name, config_dir) for name in CONFIG_FILES}
    return Configs(
        rules=loaded["rules"].model,
        costs=loaded["costs"].model,
        tax=loaded["tax"].model,
        restricted=loaded["restricted"].model,
        gate_spec=loaded["gate_spec"].model,
        calendar=loaded["calendar"].model,
        versions={name: item.version for name, item in loaded.items()},
    )
