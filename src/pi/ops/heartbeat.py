"""Dead-man's switch (WP 1.5, architecture §15).

After a successful evening run, send one bare GET to the monitor's ping URL. If the
monitor doesn't hear from us by its deadline, it emails you. The ping carries no data:
no query string, no body. Works with any monitor that accepts a plain GET (healthchecks.io,
Cronitor, Better Stack and others).

A failed ping never fails the run; the monitor noticing the silence is the point.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

TIMEOUT_S = 10.0


@dataclass(frozen=True)
class PingResult:
    ok: bool
    detail: str


def validate_url(url: str) -> str:
    parts = urlsplit(url.strip())
    if parts.scheme != "https" or not parts.netloc:
        raise ValueError("heartbeat URL must be an https:// URL")
    if parts.query or parts.fragment:
        raise ValueError("heartbeat URL must not carry a query string or fragment")
    return url.strip()


def _get(url: str) -> int:
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": "pi-heartbeat"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:  # noqa: S310 (https only)
        return int(resp.status)


def ping(url: str | None, get: Callable[[str], int] | None = None) -> PingResult:
    get = get or _get
    if not url:
        return PingResult(False, "heartbeat not configured (pi secret set heartbeat_url)")
    try:
        status = get(validate_url(url))
    except ValueError as exc:
        return PingResult(False, str(exc))
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return PingResult(False, f"ping failed: {exc}")
    if 200 <= status < 300:
        return PingResult(True, f"pinged ({status})")
    return PingResult(False, f"monitor answered {status}")
