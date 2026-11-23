"""Telegram delivery (architecture §11.2 invariants).

Telegram bot chats are not end-to-end encrypted, so a message may contain **only** a
severity, a rule name and one subject (a ticker or an ISO date). No amounts, holdings,
prices or prose. `AlertMessage` enforces that; details stay in the local UI.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

SEVERITIES = {"red": "🔴", "yellow": "🟡"}
_RULE = re.compile(r"^[A-Z][A-Z0-9_]{1,40}$")
_SUBJECT = re.compile(r"^(?:[A-Z0-9][A-Z0-9&\-]{0,19}|\d{4}-\d{2}-\d{2})$")
TIMEOUT_S = 10.0


class AlertContentError(ValueError):
    pass


@dataclass(frozen=True)
class AlertMessage:
    severity: str
    rule: str
    subject: str | None = None

    def __post_init__(self) -> None:
        if self.severity not in SEVERITIES:
            raise AlertContentError(f"unknown severity {self.severity!r}")
        if not _RULE.match(self.rule):
            raise AlertContentError(f"rule must be UPPER_SNAKE: {self.rule!r}")
        if self.subject is not None and not _SUBJECT.match(self.subject):
            raise AlertContentError(f"subject must be a ticker or ISO date: {self.subject!r}")

    def text(self) -> str:
        parts = [SEVERITIES[self.severity], self.rule]
        if self.subject:
            parts.append(self.subject)
        return " ".join(parts)


Poster = Callable[[str, bytes], int]


def _post(url: str, body: bytes) -> int:
    req = urllib.request.Request(
        url, data=body, method="POST", headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:  # noqa: S310 (https only)
        return int(resp.status)


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str, post: Poster | None = None) -> None:
        if not token or not chat_id:
            raise ValueError("token and chat_id are required")
        self._url = f"https://api.telegram.org/bot{token}/sendMessage"
        self._chat_id = chat_id
        self._post = post or _post

    def send(self, message: AlertMessage) -> None:
        body = json.dumps({"chat_id": self._chat_id, "text": message.text()}).encode()
        try:
            status = self._post(self._url, body)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            # Never let the token-bearing URL leak into logs or the database.
            raise RuntimeError(f"telegram send failed: {type(exc).__name__}") from None
        if not 200 <= status < 300:
            raise RuntimeError(f"telegram answered {status}")
