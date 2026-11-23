"""Alert engine (WP 2.4, architecture §11.2).

- **Edge-triggered:** an alert fires when a condition *becomes* true. It re-arms only
  when the rule evaluated that condition's scope today and found it false. A holding
  skipped for missing data stays armed-off, so it can't fire twice.
- **Until delivered:** an active, live alert that hasn't been delivered (Telegram down,
  bot not configured yet) is retried on every run while the condition holds.
- **Shadow mode:** a 🟡 rule is recorded but not sent for its first `shadow_days`
  trading days. A condition that started during shadow and is still true when the rule
  goes live is sent then. 🔴 rules are never in shadow.
- **Weekly review:** more than `urgent_weekly_cap` live 🔴 market alerts in 7 days raises
  one 🟡 THRESHOLD_REVIEW. Reds are never suppressed; run alerts don't count.
- One `alerts` row per firing (retries update it in place); details stay in its payload.
  Telegram gets only severity, rule and subject.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from uuid import uuid4

import duckdb

from pi.alerts.rules import SEVERITY, Condition, RuleResult
from pi.alerts.run_alerts import Notifier
from pi.alerts.telegram import AlertMessage
from pi.config import AlertRules
from pi.refdata.calendar import expected_trading_days
from pi.store.clock import IST, require_aware

RED_RULES = tuple(sorted(r for r, s in SEVERITY.items() if s == "red"))


@dataclass(frozen=True)
class Fired:
    rule_id: str
    key: str
    mode: str  # live | shadow
    delivered: bool
    retry: bool = False


def _enabled_on(con: duckdb.DuckDBPyConnection, rule_id: str, today: date) -> date:
    row = con.execute("SELECT enabled_on FROM rule_state WHERE rule_id = ?", [rule_id]).fetchone()
    if row is not None:
        return row[0]
    con.execute("INSERT INTO rule_state VALUES (?, ?)", [rule_id, today])
    return today


def _in_shadow(con, rule_id: str, today: date, cfg: AlertRules, holidays) -> bool:
    if SEVERITY[rule_id] == "red":
        return False
    enabled = _enabled_on(con, rule_id, today)
    if enabled >= today:
        return True
    days = len(expected_trading_days(enabled, today - timedelta(days=1), holidays))
    return days < cfg.shadow_days


def _new_alert(con, c: Condition, mode: str, now: datetime, rules_ver: str) -> str:
    alert_id = uuid4().hex
    con.execute(
        "INSERT INTO alerts (alert_id, rule_id, rules_ver, severity, security_id, subject, "
        "fired_at, mode, payload_json, delivered, error) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [
            alert_id,
            c.rule_id,
            rules_ver,
            c.severity,
            c.security_id,
            c.subject,
            now,
            mode,
            json.dumps(c.payload, default=str),
            False,
            None,
        ],
    )
    return alert_id


def _deliver(con, c: Condition, alert_id: str, notifier: Notifier | None) -> bool:
    message = AlertMessage(c.severity, c.rule_id, c.subject)  # content errors are bugs: raise
    delivered, error = False, None
    if notifier is None:
        error = "telegram not configured"
    else:
        try:
            notifier.send(message)
            delivered = True
        except Exception as exc:  # delivery problems must never break the run
            error = str(exc)
    con.execute(
        "UPDATE alerts SET delivered = ?, error = ? WHERE alert_id = ?",
        [delivered, error, alert_id],
    )
    con.execute(
        "UPDATE alert_state SET delivered = ? WHERE rule_id = ? AND key = ?",
        [delivered, c.rule_id, c.key],
    )
    return delivered


def process(
    con: duckdb.DuckDBPyConnection,
    results: Sequence[RuleResult],
    cfg: AlertRules,
    now: datetime,
    notifier: Notifier | None,
    rules_ver: str,
    holidays: frozenset[date] = frozenset(),
) -> list[Fired]:
    now = require_aware(now)
    today = now.astimezone(IST).date()
    by_rule = {r.rule_id: r for r in results}
    if len(by_rule) != len(results):
        raise ValueError("each rule may report only once per run")

    conditions = [c for r in results for c in r.conditions]
    true_now = {(c.rule_id, c.key) for c in conditions}
    for rule_id, key, scope in con.execute(
        "SELECT rule_id, key, scope FROM alert_state"
    ).fetchall():
        r = by_rule.get(rule_id)
        if r is not None and scope in r.evaluated and (rule_id, key) not in true_now:
            con.execute("DELETE FROM alert_state WHERE rule_id = ? AND key = ?", [rule_id, key])

    shadow = {r.rule_id: _in_shadow(con, r.rule_id, today, cfg, holidays) for r in results}
    fired: list[Fired] = []
    for c in sorted(conditions, key=lambda x: (SEVERITY[x.rule_id] != "red", x.rule_id, x.key)):
        state = con.execute(
            "SELECT alert_id, mode, delivered FROM alert_state WHERE rule_id = ? AND key = ?",
            [c.rule_id, c.key],
        ).fetchone()
        live = not shadow[c.rule_id]
        if state is None:
            mode = "live" if live else "shadow"
            alert_id = _new_alert(con, c, mode, now, rules_ver)
            con.execute(
                "INSERT INTO alert_state VALUES (?, ?, ?, ?, ?, ?, ?)",
                [c.rule_id, c.key, c.scope, now, alert_id, mode, False],
            )
            delivered = _deliver(con, c, alert_id, notifier) if live else False
            fired.append(Fired(c.rule_id, c.key, mode, delivered))
            continue
        alert_id, mode, was_delivered = state
        if was_delivered or not live:
            continue  # already sent, or its rule is still in shadow
        if mode == "shadow":  # the rule has gone live while this condition still holds
            alert_id = _new_alert(con, c, "live", now, rules_ver)
            con.execute(
                "UPDATE alert_state SET alert_id = ?, mode = 'live' WHERE rule_id = ? AND key = ?",
                [alert_id, c.rule_id, c.key],
            )
        delivered = _deliver(con, c, alert_id, notifier)
        fired.append(Fired(c.rule_id, c.key, "live", delivered, retry=mode == "live"))

    _weekly_review(con, cfg, now, notifier, rules_ver, fired)
    return fired


def _weekly_review(con, cfg: AlertRules, now: datetime, notifier, rules_ver, fired) -> None:
    week_ago = now - timedelta(days=7)
    placeholders = ", ".join("?" for _ in RED_RULES)
    # Live red market alerts count whether or not Telegram delivered them, so an
    # unconfigured bot can't hide overload. Run alerts (DATA_MISSING etc.) don't count.
    reds = con.execute(
        f"SELECT count(*) FROM alerts WHERE mode = 'live' AND fired_at > ? "
        f"AND rule_id IN ({placeholders})",
        [week_ago, *RED_RULES],
    ).fetchone()[0]
    already = con.execute(
        "SELECT count(*) FROM alerts WHERE rule_id = 'THRESHOLD_REVIEW' AND fired_at > ?",
        [week_ago],
    ).fetchone()[0]
    if reds <= cfg.urgent_weekly_cap or already:
        return
    message = AlertMessage("yellow", "THRESHOLD_REVIEW")
    delivered, error = False, None
    if notifier is not None:
        try:
            notifier.send(message)
            delivered = True
        except Exception as exc:
            error = str(exc)
    con.execute(
        "INSERT INTO alerts (alert_id, rule_id, rules_ver, severity, fired_at, mode, "
        "payload_json, delivered, error) VALUES (?,?,?,?,?,?,?,?,?)",
        [
            uuid4().hex,
            "THRESHOLD_REVIEW",
            rules_ver,
            "yellow",
            now,
            "live",
            json.dumps({"red_alerts_7d": reds, "cap": cfg.urgent_weekly_cap}),
            delivered,
            error,
        ],
    )
    fired.append(Fired("THRESHOLD_REVIEW", "review", "live", delivered))
