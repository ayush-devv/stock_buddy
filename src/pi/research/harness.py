"""The research harness: `run_trial` is the only way to run a backtest (§14.2, ADR-0014).

Before anything runs, it checks the system is in RESEARCH state:
- gate_spec.yaml is registered (`registered_at` set: you froze it);
- its hash equals the hash stored with the first trial ever run (the hook blocks agent
  edits; this is the second layer);
- a lockbox has been sealed (the backfill, WP 3.1).
If any check fails, nothing runs and nothing is logged.

Then:
1. A `started` row is **committed before** the evaluation runs. Every started row counts
   as a trial, including crashes and abandoned runs, because deflated Sharpe and PBO
   depend on the true number of attempts.
2. The evaluation runs.
3. A `finished` row (with metrics) or a `failed` row (with the error) is appended.

The log lives in data/research.duckdb (research runs are separate processes; pi.duckdb has
one writer, the evening run). Rows are never updated or deleted. Each row carries the
previous row's hash and its own, so `verify_chain` detects an edited or deleted row.
Removing the newest rows can't be detected from inside the file: this is a guard rail,
not a vault.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from uuid import uuid4

import duckdb

from pi.config import GateSpec, load_one
from pi.research import lockbox
from pi.store import db
from pi.store.clock import require_aware, utc_now

TRIALS_DDL = (
    """CREATE TABLE IF NOT EXISTS trials (
        seq INTEGER PRIMARY KEY,
        trial_id VARCHAR NOT NULL,
        kind VARCHAR NOT NULL,
        created_at TIMESTAMPTZ NOT NULL,
        config_hash VARCHAR NOT NULL,
        gate_spec_hash VARCHAR NOT NULL,
        feature_set_ver VARCHAR NOT NULL,
        data_start DATE NOT NULL,
        data_end DATE NOT NULL,
        git_sha VARCHAR NOT NULL,
        git_dirty BOOLEAN NOT NULL,
        diff_hash VARCHAR NOT NULL,
        detail_json VARCHAR NOT NULL,
        prev_hash VARCHAR NOT NULL,
        row_hash VARCHAR NOT NULL
    )""",
)
MIGRATIONS: tuple[tuple[str, ...], ...] = (lockbox.DDL + TRIALS_DDL,)
GENESIS = "0" * 64
_CHAIN_FIELDS = (
    "seq", "trial_id", "kind", "created_at", "config_hash", "gate_spec_hash",
    "feature_set_ver", "data_start", "data_end", "git_sha", "git_dirty", "diff_hash",
    "detail_json", "prev_hash",
)  # fmt: skip


class ResearchNotReady(RuntimeError):
    """The system isn't in RESEARCH state; no trial may run."""


def research_db_path(root: Path) -> Path:
    return root / "data" / "research.duckdb"


def open_research_db(root: Path) -> duckdb.DuckDBPyConnection:
    path = research_db_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = db.connect(path)
    db.migrate(con, "research", MIGRATIONS)
    return con


def canonical_hash(obj: object) -> str:
    """SHA-256 of canonical JSON: key order and whitespace don't change the hash."""
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


@dataclass(frozen=True)
class GitState:
    sha: str
    dirty: bool
    diff_hash: str  # hash of `git diff HEAD`, so uncommitted code is still identified


def git_state(root: Path) -> GitState:
    def git(*args: str) -> str:
        out = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False)
        return out.stdout if out.returncode == 0 else ""

    sha = git("rev-parse", "HEAD").strip() or "unknown"
    dirty = bool(git("status", "--porcelain").strip())
    diff = git("diff", "HEAD")
    return GitState(sha, dirty, hashlib.sha256(diff.encode()).hexdigest())


# --------------------------------------------------------------------------- the log


def _row_hash(fields: Mapping[str, object]) -> str:
    return canonical_hash({k: fields[k] for k in _CHAIN_FIELDS})


def _append(con: duckdb.DuckDBPyConnection, fields: dict[str, object]) -> None:
    last = con.execute("SELECT seq, row_hash FROM trials ORDER BY seq DESC LIMIT 1").fetchone()
    fields = {
        **fields,
        "seq": (last[0] + 1) if last else 1,
        "prev_hash": last[1] if last else GENESIS,
    }
    fields["created_at"] = require_aware(fields["created_at"]).isoformat()  # type: ignore[arg-type]
    fields["data_start"] = str(fields["data_start"])
    fields["data_end"] = str(fields["data_end"])
    fields["row_hash"] = _row_hash(fields)
    cols = [*_CHAIN_FIELDS, "row_hash"]
    con.execute(
        f"INSERT INTO trials ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
        [fields[c] for c in cols],
    )


def verify_chain(con: duckdb.DuckDBPyConnection) -> list[str]:
    """Problems found in the trial log; an empty list means the chain is intact."""
    rows = con.execute(
        f"SELECT {', '.join([*_CHAIN_FIELDS, 'row_hash'])} FROM trials ORDER BY seq"
    ).fetchall()
    problems, prev, expected_seq = [], GENESIS, 1
    for row in rows:
        fields = dict(zip([*_CHAIN_FIELDS, "row_hash"], row, strict=True))
        fields["created_at"] = require_aware(fields["created_at"]).isoformat()
        fields["data_start"], fields["data_end"] = (
            str(fields["data_start"]),
            str(fields["data_end"]),
        )
        if fields["seq"] != expected_seq:
            problems.append(f"row {expected_seq} is missing (found {fields['seq']})")
            expected_seq = fields["seq"]
        if fields["prev_hash"] != prev:
            problems.append(f"row {fields['seq']}: chain broken (previous row changed or removed)")
        if _row_hash(fields) != fields["row_hash"]:
            problems.append(f"row {fields['seq']}: contents were edited")
        prev, expected_seq = fields["row_hash"], expected_seq + 1
    return problems


def trial_count(con: duckdb.DuckDBPyConnection) -> int:
    """Every started trial counts, including failed and abandoned ones."""
    return int(con.execute("SELECT count(*) FROM trials WHERE kind = 'started'").fetchone()[0])


# --------------------------------------------------------------------------- run_trial


def preflight(con: duckdb.DuckDBPyConnection, spec: GateSpec, spec_hash: str) -> lockbox.Seal:
    if not spec.is_registered:
        raise ResearchNotReady(
            "gate_spec.yaml is not registered. Freeze it (set registered_at) before any trial."
        )
    seal = lockbox.current_seal(con)
    if seal is None or seal.burned:
        raise ResearchNotReady("no unused lockbox is sealed yet (the backfill, WP 3.1, seals it)")
    # The spec is fixed for the life of a lockbox: compare with the first trial run since
    # the current seal. A new spec is allowed only with a new lockbox (§14: FAILED ->
    # RESEARCH with a new pre-registered spec and a new lockbox).
    first = con.execute(
        "SELECT gate_spec_hash FROM trials WHERE created_at >= ? ORDER BY seq LIMIT 1",
        [seal.sealed_at],
    ).fetchone()
    if first is not None and first[0] != spec_hash:
        raise ResearchNotReady(
            "gate_spec.yaml changed after trials began on this lockbox. A changed spec needs "
            "a new lockbox."
        )
    problems = verify_chain(con)
    if problems:
        raise ResearchNotReady(f"trial log failed verification: {problems[0]}")
    return seal


def run_trial(
    root: Path,
    config: Mapping[str, object],
    feature_set_ver: str,
    data_window: tuple[date, date],
    evaluate: Callable[[], Mapping[str, float]],
    now: Callable[[], datetime] = utc_now,
    git: Callable[[Path], GitState] = git_state,
) -> Mapping[str, float]:
    """Run one backtest under the controls above and return its metrics."""
    loaded = load_one("gate_spec", root / "config")
    spec: GateSpec = loaded.model
    con = open_research_db(root)
    try:
        seal = preflight(con, spec, loaded.version)
        start, end = data_window
        if end >= seal.start:
            raise ResearchNotReady(
                f"data window ends {end}, inside the lockbox (from {seal.start})"
            )
        g = git(root)
        base = {
            "trial_id": uuid4().hex,
            "config_hash": canonical_hash(config),
            "gate_spec_hash": loaded.version,
            "feature_set_ver": feature_set_ver,
            "data_start": start,
            "data_end": end,
            "git_sha": g.sha,
            "git_dirty": g.dirty,
            "diff_hash": g.diff_hash,
        }
        _append(
            con,
            {
                **base,
                "kind": "started",
                "created_at": now(),
                "detail_json": json.dumps({"config": config}, sort_keys=True, default=str),
            },
        )
        try:
            metrics = dict(evaluate())
        except Exception as exc:
            _append(
                con,
                {
                    **base,
                    "kind": "failed",
                    "created_at": now(),
                    "detail_json": json.dumps({"error": repr(exc)}),
                },
            )
            raise
        _append(
            con,
            {
                **base,
                "kind": "finished",
                "created_at": now(),
                "detail_json": json.dumps({"metrics": metrics}, sort_keys=True),
            },
        )
        return metrics
    finally:
        con.close()
