"""Alert engine (WP 2.4, architecture §11.2).

For each rule evaluated today:
- **Edge-triggered:** an alert fires when a condition *becomes* true, and re-arms once it
  is false again. A drawdown that lasts three weeks alerts once, not fifteen times.
- **Shadow mode:** a 🟡 rule is recorded but not sent for its first `shadow_days` weekdays
  after it was first evaluated, so its threshold can be judged by how often it fires.
  🔴 rules are never in shadow.
- **Weekly cap:** more than `urgent_weekly_cap` 🔴 alerts delivered in 7 days raises one
  🟡 THRESHOLD_REVIEW. Red alerts themselves are never suppressed.
- Everything is recorded in `alerts` (mode shadow or live, payload for the local UI).
  Telegram gets only severity, rule and subject.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from uuid import uuid4

import duckdb

from pi.alerts.rules import SEVERITY, Condition
from pi.alerts.run_alerts import Notifier
from pi.alerts.telegram import AlertMessage
from pi.config import AlertRules
from pi.store.clock import IST, require_aware


def weekdays_between(start: date, end: date) -> int:
    """Weekdays in [start, end): the shadow clock (holidays ignored, slightly conservative)."""
    days, d = 0, start
    while d < end:
        if d.weekday() < 5:
            days += 1
        d += timedelta(days=1)
    return days


@dataclass(frozen=True)
class Fired:
    rule_id: str
    key: str
    mode: str  # live | shadow
    delivered: bool


def _enabled_on(con: duckdb.DuckDBPyConnection, rule_id: str, today: date) -> date:
    row = con.execute("SELECT enabled_on FROM rule_state WHERE rule_id = ?", [rule_id]).fetchone()
    if row is not None:
        return row[0]
    con.execute("INSERT INTO rule_state VALUES (?, ?)", [rule_id, today])
    return today


def _record(con, c: Condition, mode: str, delivered: bool, error: str | None, now, rules_ver):
    con.execute(
        "INSERT INTO alerts (alert_id, rule_id, rules_ver, severity, security_id, subject, "
        "fired_at, mode, payload_json, delivered, error) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [
            uuid4().hex,
            c.rule_id,
            rules_ver,
            c.severity,
            c.security_id,
            c.subject,
            now,
            mode,
            json.dumps(c.payload, default=str),
            delivered,
            error,
        ],
    )


def process(
    con: duckdb.DuckDBPyConnection,
    evaluated: Iterable[str],
    conditions: Sequence[Condition],
    cfg: AlertRules,
    now: datetime,
    notifier: Notifier | None,
    rules_ver: str,
) -> list[Fired]:
    """Apply edge-triggering, shadow mode and delivery to today's true conditions.
    `evaluated` lists every rule that ran today, so conditions that went false re-arm."""
    now = require_aware(now)
    today = now.astimezone(IST).date()
    evaluated = set(evaluated)
    unknown = {c.rule_id for c in conditions} - evaluated
    if unknown:
        raise ValueError(f"conditions from rules not marked as evaluated: {sorted(unknown)}")

    true_now = {(c.rule_id, c.key) for c in conditions}
    active = set(con.execute("SELECT rule_id, key FROM alert_state").fetchall())
    for rule_id, key in active - true_now:
        if rule_id in evaluated:  # re-arm only rules that actually ran today
            con.execute("DELETE FROM alert_state WHERE rule_id = ? AND key = ?", [rule_id, key])

    fired: list[Fired] = []
    for c in sorted(conditions, key=lambda x: (SEVERITY[x.rule_id] != "red", x.rule_id, x.key)):
        if (c.rule_id, c.key) in active:
            continue  # still true from before: already alerted
        con.execute("INSERT INTO alert_state VALUES (?, ?, ?)", [c.rule_id, c.key, now])
        enabled = _enabled_on(con, c.rule_id, today)
        shadow = c.severity == "yellow" and weekdays_between(enabled, today) < cfg.shadow_days
        delivered, error = False, None
        if not shadow:
            if notifier is None:
                error = "telegram not configured"
            else:
                try:
                    notifier.send(AlertMessage(c.severity, c.rule_id, c.subject))
                    delivered = True
                except Exception as exc:  # delivery must never break the run
                    error = str(exc)
        _record(con, c, "shadow" if shadow else "live", delivered, error, now, rules_ver)
        fired.append(Fired(c.rule_id, c.key, "shadow" if shadow else "live", delivered))

    for rule_id in evaluated:  # start the shadow clock even on quiet days
        _enabled_on(con, rule_id, today)

    _weekly_review(con, cfg, now, notifier, rules_ver, fired)
    return fired


def _weekly_review(con, cfg: AlertRules, now: datetime, notifier, rules_ver, fired) -> None:
    week_ago = now - timedelta(days=7)
    # Live reds count whether or not Telegram delivered them: an unconfigured bot must not
    # hide alert overload.
    reds = con.execute(
        "SELECT count(*) FROM alerts WHERE severity = 'red' AND mode = 'live' "
        "AND fired_at > ? AND rule_id <> 'THRESHOLD_REVIEW'",
        [week_ago],
    ).fetchone()[0]
    already = con.execute(
        "SELECT count(*) FROM alerts WHERE rule_id = 'THRESHOLD_REVIEW' AND fired_at > ?",
        [week_ago],
    ).fetchone()[0]
    if reds > cfg.urgent_weekly_cap and not already:
        payload = {"red_alerts_7d": reds, "cap": cfg.urgent_weekly_cap}
        delivered, error = False, None
        if notifier is not None:
            try:
                notifier.send(AlertMessage("yellow", "THRESHOLD_REVIEW"))
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
                json.dumps(payload),
                delivered,
                error,
            ],
        )
        fired.append(Fired("THRESHOLD_REVIEW", "review", "live", delivered))
