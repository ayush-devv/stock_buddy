"""Alerts about the evening run itself (WP 1.5): 🔴 DATA_MISSING, NO_DATA, RUN_FAILED.

Every alert is recorded in the `alerts` table whether or not it could be delivered.
The same rule and subject is not re-sent within COOLDOWN, so catch-up runs (wake, login)
don't repeat yesterday's message.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol
from uuid import uuid4

import duckdb

from pi.alerts.telegram import AlertMessage
from pi.store.clock import require_aware

COOLDOWN = timedelta(hours=20)

# Columns follow architecture §7. security_id, rules_ver, payload_json and acked_at are
# filled by the alert rules of WP 2.4 and scored per §11.4; run alerts leave them NULL.
DDL = (
    """CREATE TABLE IF NOT EXISTS alerts (
        alert_id VARCHAR PRIMARY KEY,
        rule_id VARCHAR NOT NULL,
        rules_ver VARCHAR,
        severity VARCHAR NOT NULL,
        security_id INTEGER,
        subject VARCHAR,
        fired_at TIMESTAMPTZ NOT NULL,
        mode VARCHAR NOT NULL,
        payload_json VARCHAR,
        delivered BOOLEAN NOT NULL,
        error VARCHAR,
        acked_at TIMESTAMPTZ
    )""",
)
MIGRATIONS: tuple[tuple[str, ...], ...] = (DDL,)


class Notifier(Protocol):
    def send(self, message: AlertMessage) -> None: ...


def _recently_delivered(
    con: duckdb.DuckDBPyConnection, message: AlertMessage, now: datetime
) -> bool:
    row = con.execute(
        "SELECT count(*) FROM alerts WHERE rule_id = ? AND subject IS NOT DISTINCT FROM ? "
        "AND delivered AND fired_at > ?",
        [message.rule, message.subject, now - COOLDOWN],
    ).fetchone()
    return bool(row and row[0])


def fire(
    con: duckdb.DuckDBPyConnection,
    message: AlertMessage,
    notifier: Notifier | None,
    now: datetime,
) -> str:
    """Record and (unless in cooldown) deliver one live alert. Returns what happened."""
    now = require_aware(now)
    if _recently_delivered(con, message, now):
        return "cooldown"
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
        "INSERT INTO alerts (alert_id, rule_id, severity, subject, fired_at, mode, delivered, "
        "error) VALUES (?, ?, ?, ?, ?, 'live', ?, ?)",
        [uuid4().hex, message.rule, message.severity, message.subject, now, delivered, error],
    )
    return "delivered" if delivered else f"not delivered: {error}"


def for_run(status: str, first_missing: str | None) -> AlertMessage | None:
    """The alert (if any) for a finished run's status."""
    if status == "paused_missing":
        return AlertMessage("red", "DATA_MISSING", first_missing)
    if status == "no_data":
        return AlertMessage("red", "NO_DATA")
    if status == "failed":
        return AlertMessage("red", "RUN_FAILED")
    return None  # ok, noop, and refused (you asked for it at the keyboard)
