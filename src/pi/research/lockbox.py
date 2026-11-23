"""The only module that knows where the sealed lockbox lives (architecture §14.2).

Phase 0 defines the location only. The sealing (WP 3.1) and the one-time evaluation run by
the user (WP 3.7) are added here later. No other module may import this one; an
import-linter contract and tests/test_research_controls.py enforce that.
"""

from __future__ import annotations

from pathlib import Path

LOCKBOX_DIRNAME = "lockbox"


def lockbox_dir(data_dir: Path) -> Path:
    return data_dir / LOCKBOX_DIRNAME


def is_inside_lockbox(path: Path, data_dir: Path) -> bool:
    """True if `path` resolves to somewhere under the lockbox directory."""
    return path.resolve().is_relative_to(lockbox_dir(data_dir).resolve())
