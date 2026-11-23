"""The launchd LaunchAgent that runs `pi run` every evening (ADR-0003).

- StartCalendarInterval 19:00 local time. If the Mac was asleep, launchd runs the missed
  job once on wake. RunAtLoad runs it at login if the Mac was off at 19:00.
- It calls the project's own virtualenv `pi` directly, because launchd's PATH is minimal.
- Installing writes ~/Library/LaunchAgents/<label>.plist and loads it with launchctl.
  You run the install yourself: it changes files outside the project.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path

LABEL = "com.pi-portfolio.evening-run"
RUN_HOUR, RUN_MINUTE = 19, 0


def plist_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def render(root: Path) -> bytes:
    root = root.resolve()
    logs = root / "data" / "logs"
    spec = {
        "Label": LABEL,
        "ProgramArguments": [str(root / ".venv" / "bin" / "pi"), "run"],
        "WorkingDirectory": str(root),
        "EnvironmentVariables": {"PI_HOME": str(root)},
        "StartCalendarInterval": {"Hour": RUN_HOUR, "Minute": RUN_MINUTE},
        "RunAtLoad": True,
        "ProcessType": "Background",
        "StandardOutPath": str(logs / "evening-run.out.log"),
        "StandardErrorPath": str(logs / "evening-run.err.log"),
    }
    return plistlib.dumps(spec)


def local_tz_name() -> str:
    return str(datetime.now().astimezone().tzname())


Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def _run(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(args), capture_output=True, text=True, check=False)


def install(root: Path, home: Path | None = None, runner: Runner = _run) -> Path:
    exe = root.resolve() / ".venv" / "bin" / "pi"
    if not exe.exists():
        raise FileNotFoundError(f"{exe} not found; run `uv sync` in {root} first")
    (root / "data" / "logs").mkdir(parents=True, exist_ok=True)
    path = plist_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    domain = f"gui/{os.getuid()}"
    runner(["launchctl", "bootout", f"{domain}/{LABEL}"])  # ignore "not loaded"
    path.write_bytes(render(root))
    result = runner(["launchctl", "bootstrap", domain, str(path)])
    if result.returncode != 0:
        raise RuntimeError(f"launchctl bootstrap failed: {result.stderr.strip()}")
    return path


def uninstall(home: Path | None = None, runner: Runner = _run) -> bool:
    path = plist_path(home)
    runner(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"])
    if path.exists():
        path.unlink()
        return True
    return False
