from __future__ import annotations

import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def root() -> Path:
    return ROOT


@pytest.fixture
def config_copy(tmp_path: Path) -> Path:
    """A writable copy of config/ for tests that change files."""
    dest = tmp_path / "config"
    shutil.copytree(ROOT / "config", dest)
    return dest
