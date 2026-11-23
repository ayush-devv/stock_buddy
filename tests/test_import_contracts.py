"""WP 0.2: the import contracts hold, and they really fail when violated."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

LINT_IMPORTS = str(Path(sys.executable).parent / "lint-imports")
LOADER = "lock" + "box"  # assembled at runtime; see .claude/hooks/research_guard.py


def _lint(cwd: Path, src: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": str(src)}
    return subprocess.run(
        [LINT_IMPORTS, "--config", str(cwd / "pyproject.toml"), "--no-cache"],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_contracts_hold_on_the_real_tree(root: Path) -> None:
    result = _lint(root, root / "src")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 broken" in result.stdout


def test_planted_violations_break_both_contracts(root: Path, tmp_path: Path) -> None:
    src = tmp_path / "src"
    shutil.copytree(root / "src", src, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy(root / "pyproject.toml", tmp_path / "pyproject.toml")

    # A low layer importing a high one, and a non-research module reaching the loader.
    (src / "pi" / "store" / "_planted.py").write_text("import pi.ui\n")
    (src / "pi" / "decide" / "_planted.py").write_text(f"from pi.research import {LOADER}\n")

    result = _lint(tmp_path, src)
    assert result.returncode != 0, result.stdout
    assert "Layering: lower layers never import higher ones BROKEN" in result.stdout
    assert "Only pi.research may reach the" in result.stdout
    assert "2 broken" in result.stdout
