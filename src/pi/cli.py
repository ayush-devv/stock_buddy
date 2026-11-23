"""Command-line entry point: `pi status`, `pi run` (WP 0.4, WP 1.4)."""

from __future__ import annotations

import json
from datetime import date

import typer

from pi import pipeline
from pi.config import CONFIG_FILES, ConfigError, load_one, project_root
from pi.store import db

app = typer.Typer(no_args_is_help=True, add_completion=False)


@app.command()
def status() -> None:
    """Show config versions and run state (read from the snapshot, never the live DB)."""
    root = project_root()
    paths = pipeline.Paths(root)
    typer.echo(f"project root: {root}")

    failed = False
    typer.echo("config:")
    for name in CONFIG_FILES:
        try:
            loaded = load_one(name, paths.config_dir)
        except ConfigError as exc:
            failed = True
            typer.echo(f"  {name:<11} INVALID  {exc}")
            continue
        typer.echo(f"  {name:<11} {loaded.version}")
        if name == "gate_spec" and not loaded.model.is_registered:
            typer.echo("              draft: not registered yet (freeze before Phase 3)")
        if name == "costs" and not all(s.verified for s in loaded.model.schedules):
            typer.echo("              unverified charges (confirm in WP 1.9)")
        if name == "calendar" and not loaded.model.holidays:
            typer.echo("              no holidays listed: add NSE's holiday circular dates")

    if not paths.snapshot_path.exists():
        typer.echo("runs:       none yet (`pi run` creates the database)")
        typer.echo("gate_state: NOT_EVALUATED")
        raise typer.Exit(code=1 if failed else 0)

    con = db.connect(paths.snapshot_path, read_only=True)
    try:
        state = dict(con.execute("SELECT key, value FROM system_state").fetchall())
        recent = con.execute(
            "SELECT trade_date, status, detail FROM run_log "
            "ORDER BY finished_at DESC, trade_date DESC NULLS LAST LIMIT 5"
        ).fetchall()
        row = con.execute("SELECT count(*) FROM refdata_issues WHERE resolved_at IS NULL")
        issues = row.fetchone()[0]
    finally:
        con.close()

    last_status, last_at = state.get("last_run_status", "-"), state.get("last_run_at", "-")
    typer.echo(f"last run:   {last_status} at {last_at}")
    typer.echo(f"complete through: {state.get(pipeline.LAST_COMPLETE, '-')}")
    streak = state.get("clean_streak", "0")
    typer.echo(f"clean streak: {streak} / {pipeline.STREAK_TARGET} trading days")
    corrections = json.loads(state.get("corrections_pending") or "[]")
    if corrections:
        typer.echo(
            "corrections arrived after completion (acknowledge with `pi run --date`; "
            "security-ID history for that day is not re-applied yet): " + ", ".join(corrections)
        )
    if issues:
        typer.echo(f"refdata issues open: {issues} (new ISINs needing a link or confirmation)")
    typer.echo("recent:")
    for trade_date, st, detail in recent:
        label = trade_date.isoformat() if trade_date else "-"
        typer.echo(f"  {label:<10} {st}")
        if st == "paused_missing" and detail:
            typer.echo(f"             {json.loads(detail)}")
    typer.echo("gate_state: NOT_EVALUATED")
    if failed:
        raise typer.Exit(code=1)


@app.command()
def run(
    trade_date: str = typer.Option(
        None, "--date", help="Re-process one trade date (YYYY-MM-DD) instead of all pending."
    ),
) -> None:
    """Process every pending trade date. Exit 0 ok, 1 failed, 3 paused on missing data."""
    force = None
    if trade_date is not None:
        try:
            force = date.fromisoformat(trade_date)
        except ValueError as exc:
            raise typer.BadParameter("expected YYYY-MM-DD", param_hint="--date") from exc
    try:
        result = pipeline.run(project_root(), force_date=force)
    except pipeline.RunLocked as exc:
        typer.echo(f"pi run: {exc}", err=True)
        raise typer.Exit(code=pipeline.EXIT_FAILED) from exc
    except ConfigError as exc:
        typer.echo(f"pi run: invalid config: {exc}", err=True)
        raise typer.Exit(code=pipeline.EXIT_FAILED) from exc

    processed = ", ".join(d.isoformat() for d in result.processed) or "none"
    typer.echo(f"status: {result.status}; processed: {processed}")
    typer.echo(f"clean streak: {result.streak} / {pipeline.STREAK_TARGET}")
    if result.message:
        typer.echo(result.message, err=result.exit_code != 0)
    if result.missing:
        typer.echo("missing: " + ", ".join(d.isoformat() for d in result.missing), err=True)
    raise typer.Exit(code=result.exit_code)


if __name__ == "__main__":  # pragma: no cover
    app()
