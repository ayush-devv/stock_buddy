from __future__ import annotations

import shutil
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import keyring
import pytest
from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError

ROOT = Path(__file__).resolve().parents[1]


class MemoryKeyring(KeyringBackend):
    """In-memory keyring so tests never read or write your real macOS Keychain."""

    priority = 1  # type: ignore[assignment]

    def __init__(self) -> None:
        super().__init__()
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if (service, username) not in self.store:
            raise PasswordDeleteError(username)
        del self.store[(service, username)]


@pytest.fixture(autouse=True)
def memory_keyring() -> Iterator[MemoryKeyring]:
    previous = keyring.get_keyring()
    backend = MemoryKeyring()
    keyring.set_keyring(backend)
    try:
        yield backend
    finally:
        keyring.set_keyring(previous)


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests must never reach the internet (Telegram, heartbeat monitors)."""

    def refuse(*args, **kwargs):
        raise AssertionError("network access attempted in a test")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)


@pytest.fixture
def root() -> Path:
    return ROOT


@pytest.fixture
def config_copy(tmp_path: Path) -> Path:
    """A writable copy of config/ for tests that change files."""
    dest = tmp_path / "config"
    shutil.copytree(ROOT / "config", dest)
    return dest
