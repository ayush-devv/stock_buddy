"""Secrets live in the macOS login Keychain, never in the repo or config (architecture §15).

A LaunchAgent can read the login Keychain only while you are logged in, which matches
when the evening run happens (ADR-0003).
"""

from __future__ import annotations

import contextlib

import keyring
import keyring.errors

SERVICE = "pi-portfolio"

# name -> what it is (shown by `pi secret status`, never the value)
KNOWN: dict[str, str] = {
    "telegram_bot_token": "Telegram bot token from @BotFather",
    "telegram_chat_id": "your Telegram chat id (the bot messages only you)",
    "heartbeat_url": "ping URL from your heartbeat monitor (for example healthchecks.io)",
}


class SecretError(Exception):
    pass


def _check(name: str) -> None:
    if name not in KNOWN:
        raise SecretError(f"unknown secret {name!r}; known: {', '.join(KNOWN)}")


def get(name: str) -> str | None:
    _check(name)
    try:
        return keyring.get_password(SERVICE, name)
    except keyring.errors.KeyringError as exc:
        raise SecretError(f"Keychain unavailable: {exc}") from exc


def put(name: str, value: str) -> None:
    _check(name)
    value = value.strip()
    if not value:
        raise SecretError("empty value")
    keyring.set_password(SERVICE, name, value)


def delete(name: str) -> None:
    _check(name)
    with contextlib.suppress(keyring.errors.PasswordDeleteError):
        keyring.delete_password(SERVICE, name)


def status() -> dict[str, bool]:
    """Which secrets are set. Values are never returned here."""
    return {name: get(name) is not None for name in KNOWN}
