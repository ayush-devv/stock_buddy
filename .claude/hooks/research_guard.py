#!/usr/bin/env python3
"""PreToolUse guard for coding agents (implementation plan WP 0.5, architecture §14.2).

It runs on every tool call and blocks the call (exit code 2) when it would:
  1. touch anything under data/lockbox/
  2. write config/gate_spec.yaml (shell commands that mention it are blocked too; use Read)
  3. run `pi gate evaluate`, which only the user may run
  4. change this hook, .claude/settings.json or .claude/settings.local.json, which would
     switch the guard off (local settings load last, so `disableAllHooks` there wins)

Only target fields are inspected (file_path, path, paths, notebook_path, cwd, ops[].path and
shell commands), never file contents, so editing code *about* these paths still works.

This is a guard rail, not a sandbox: a deliberately obfuscated command can get past it. The
import-linter contract and tests/test_research_controls.py are the second layer.
Standard library only; it must not depend on the project's virtualenv.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterator
from typing import Any

WRITE_TOOLS = {
    "Write",
    "Edit",
    "MultiEdit",
    "NotebookEdit",
    "mcp__lean-ctx__ctx_patch",
    "mcp__lean-ctx__ctx_edit",
}
PATH_KEYS = ("file_path", "path", "paths", "notebook_path", "cwd")
COMMAND_KEYS = ("command",)

LOCKBOX = re.compile(r"(^|/)data/lockbox(/|$)")
GATE_SPEC = re.compile(r"(^|/)config/gate_spec\.ya?ml$")
GATE_SPEC_MENTION = re.compile(r"gate_spec\.ya?ml")
GATE_EVALUATE = re.compile(r"\bgate\s+evaluate\b")
CONTROL_FILES = re.compile(r"(^|/)\.claude/(settings(\.local)?\.json|hooks(/|$))")
# Shell commands: no anchors, because paths there follow spaces, quotes or `=`.
LOCKBOX_IN_CMD = re.compile(r"data/lockbox\b")
CONTROL_FILES_IN_CMD = re.compile(r"\.claude/(settings(\.local)?\.json|hooks\b)")


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _norm(path: str) -> str:
    return path.replace("\\", "/")


def targets(tool_input: dict[str, Any]) -> list[str]:
    found = [_norm(s) for key in PATH_KEYS for s in _strings(tool_input.get(key))]
    for op in tool_input.get("ops") or []:
        if isinstance(op, dict):
            found.extend(_norm(s) for s in _strings(op.get("path")))
    return found


def commands(tool_input: dict[str, Any]) -> list[str]:
    return [s for key in COMMAND_KEYS for s in _strings(tool_input.get(key))]


def check(payload: dict[str, Any]) -> str | None:
    """Return the reason to block, or None to allow."""
    tool = str(payload.get("tool_name", ""))
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return None
    paths = targets(tool_input)
    cmds = commands(tool_input)
    in_data_dir = any(re.search(r"(^|/)data/?$", p) for p in paths)

    for p in paths:
        if LOCKBOX.search(p):
            return f"{p} is inside the sealed lockbox (architecture §14.2)."
    for c in cmds:
        if LOCKBOX_IN_CMD.search(_norm(c)) or (in_data_dir and "lockbox" in c):
            return "Shell access to the sealed lockbox is not allowed (architecture §14.2)."
        if GATE_EVALUATE.search(c):
            return "Only the user may run `pi gate evaluate` (architecture §14.2)."
        if GATE_SPEC_MENTION.search(c):
            return "Shell commands may not touch gate_spec.yaml; read it with a read tool instead."
        if CONTROL_FILES_IN_CMD.search(_norm(c)):
            return "Shell commands may not touch .claude settings files or .claude/hooks/."

    if tool in WRITE_TOOLS:
        for p in paths:
            if GATE_SPEC.search(p):
                return "gate_spec.yaml is pre-registered; only the user edits it (§14.2)."
            if CONTROL_FILES.search(p):
                return "Agents may not change the research guard or project settings."
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        # Fail open with a visible error rather than blocking every tool in the session.
        print(f"research_guard: could not parse hook input: {exc}", file=sys.stderr)
        return 1
    reason = check(payload) if isinstance(payload, dict) else None
    if reason:
        print(f"Blocked by research_guard: {reason}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
