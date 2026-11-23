"""WP 0.5: controls on coding agents (architecture §14.2).

1. No code outside src/pi/research/ names the sealed holdout directory as a path.
2. The PreToolUse hook blocks the actions agents may not take, and allows normal work.

Forbidden strings are assembled at runtime so this file's own commands never trip the hook.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

LB = "lock" + "box"
DATA_LB = f"data/{LB}"
GATE = "gate_" + "spec.yaml"
ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / ".claude" / "hooks" / "research_guard.py"


# --------------------------------------------------------------------------- 1. source scan


def holdout_path_literals(source: str) -> list[int]:
    """Line numbers of string literals that use the holdout dir as a path component."""
    hits = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            parts = node.value.replace("\\", "/").split("/")
            if LB in parts:
                hits.append(node.lineno)
    return hits


def _code_files() -> list[Path]:
    files = list((ROOT / "src").rglob("*.py"))
    research_dir = ROOT / "research"
    if research_dir.is_dir():
        files += list(research_dir.rglob("*.py")) + list(research_dir.rglob("*.ipynb"))
    return files


def test_only_research_package_names_the_holdout_path() -> None:
    allowed = ROOT / "src" / "pi" / "research"
    offenders = []
    for path in _code_files():
        if path.is_relative_to(allowed):
            continue
        text = path.read_text()
        if path.suffix == ".ipynb":
            if DATA_LB in text:
                offenders.append(f"{path}")
        elif lines := holdout_path_literals(text):
            offenders.append(f"{path}:{lines}")
    assert offenders == []


@pytest.mark.parametrize(
    "planted",
    [
        f'Path("data") / "{LB}"',
        f'open("{DATA_LB}/2025.parquet")',
        f'f"{{root}}/{LB}/x"',
    ],
)
def test_scanner_catches_planted_paths(planted: str) -> None:
    assert holdout_path_literals(planted) == [1]


def test_scanner_ignores_prose_and_identifiers() -> None:
    assert holdout_path_literals(f'"""the {LB} is sealed"""\n{LB}_years = 2\n') == []


# --------------------------------------------------------------------------- 2. hook


def run_hook(tool_name: str, tool_input: dict[str, object]) -> subprocess.CompletedProcess[str]:
    payload = json.dumps({"tool_name": tool_name, "tool_input": tool_input})
    return subprocess.run(
        [sys.executable, "-I", str(HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        check=False,
    )


BLOCKED = [
    ("Read", {"file_path": f"/repo/{DATA_LB}/2025.parquet"}),
    ("mcp__lean-ctx__ctx_read", {"path": f"{DATA_LB}/x.parquet"}),
    ("mcp__lean-ctx__ctx_read", {"paths": ["src/pi/config.py", f"{DATA_LB}/a"]}),
    ("mcp__lean-ctx__ctx_tree", {"path": f"/repo/{DATA_LB}"}),
    ("Bash", {"command": f"cat {DATA_LB}/2025.parquet"}),
    ("mcp__lean-ctx__ctx_shell", {"command": f"ls {LB}", "cwd": "/repo/data"}),
    ("Bash", {"command": "uv run pi gate evaluate --" + LB}),
    ("mcp__lean-ctx__ctx_shell", {"command": "pi  gate   evaluate"}),
    ("Edit", {"file_path": f"/repo/config/{GATE}", "old_string": "a", "new_string": "b"}),
    ("Write", {"file_path": f"config/{GATE}", "content": "x"}),
    ("mcp__lean-ctx__ctx_patch", {"ops": [{"path": f"/repo/config/{GATE}"}]}),
    ("Bash", {"command": f"sed -i '' 's/0.57/0.60/' config/{GATE}"}),
    ("Write", {"file_path": "/repo/.claude/settings.json", "content": "{}"}),
    # Local settings load last; {"disableAllHooks": true} there would switch the guard off.
    ("Write", {"file_path": "/repo/.claude/settings.local.json", "content": "{}"}),
    ("Bash", {"command": "echo '{}' > .claude/settings.local.json"}),
    ("Edit", {"file_path": "/repo/.claude/hooks/research_guard.py"}),
    ("Bash", {"command": "rm .claude/hooks/research_guard.py"}),
]

ALLOWED = [
    ("Read", {"file_path": f"/repo/config/{GATE}"}),
    ("Read", {"file_path": "/repo/.claude/settings.json"}),
    ("Edit", {"file_path": "/repo/src/pi/research/" + LB + ".py"}),
    ("Edit", {"file_path": "/repo/src/pi/config.py", "new_string": DATA_LB}),
    ("Bash", {"command": "uv run pytest -q"}),
    ("mcp__lean-ctx__ctx_shell", {"command": "git status", "cwd": "/repo"}),
    ("mcp__lean-ctx__ctx_read", {"path": "/repo/data/inbox/bhav.csv"}),
    ("Write", {"file_path": "/repo/config/rules.yaml", "content": "x"}),
]


@pytest.mark.parametrize(("tool", "tool_input"), BLOCKED)
def test_hook_blocks(tool: str, tool_input: dict[str, object]) -> None:
    result = run_hook(tool, tool_input)
    assert result.returncode == 2, result.stderr
    assert "Blocked by research_guard" in result.stderr


@pytest.mark.parametrize(("tool", "tool_input"), ALLOWED)
def test_hook_allows_normal_work(tool: str, tool_input: dict[str, object]) -> None:
    result = run_hook(tool, tool_input)
    assert result.returncode == 0, result.stderr


def test_hook_fails_open_visibly_on_garbage_input() -> None:
    result = subprocess.run(
        [sys.executable, "-I", str(HOOK)],
        input="not json",
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "could not parse" in result.stderr
