"""Command-line entry point: `pi status`, `pi run` (WP 0.4)."""

from __future__ import annotations

from datetime import date

import typer

from pi.config import CONFIG_FILES, ConfigError, load_one, project_root

app = typer.Typer(no_args_is_help=True, add_completion=False)

DATA_SUBDIRS = ("inbox", "processed", "quarantine", "raw")


@app.command()
def status() -> None:
    """Show config versions, data folders and run state."""
    root = project_root()
    typer.echo(f"project root: {root}")

    failed = False
    typer.echo("config:")
    for name in CONFIG_FILES:
        try:
            loaded = load_one(name, root / "config")
        except ConfigError as exc:
            failed = True
            typer.echo(f"  {name:<11} INVALID  {exc}")
            continue
        typer.echo(f"  {name:<11} {loaded.version}")
        if name == "gate_spec" and not loaded.model.is_registered:
            typer.echo("              draft: not registered yet (freeze before Phase 3)")
        if name == "costs" and not all(s.verified for s in loaded.model.schedules):
            typer.echo("              unverified charges (confirm in WP 1.9)")

    typer.echo("data:")
    data_dir = root / "data"
    for sub in DATA_SUBDIRS:
        mark = "ok" if (data_dir / sub).is_dir() else "missing (created in WP 1.2)"
        typer.echo(f"  {sub:<11} {mark}")

    typer.echo("runs:       none yet; the evening run arrives in WP 1.4")
    typer.echo("gate_state: NOT_EVALUATED")
    if failed:
        raise typer.Exit(code=1)


@app.command()
def run(
    trade_date: str = typer.Option(None, "--date", help="Trade date to process, YYYY-MM-DD."),
) -> None:
    """Process pending trade dates (not implemented until WP 1.4)."""
    if trade_date is not None:
        try:
            date.fromisoformat(trade_date)
        except ValueError as exc:
            raise typer.BadParameter("expected YYYY-MM-DD", param_hint="--date") from exc
    typer.echo("pi run: not implemented until WP 1.4", err=True)
    # Non-zero so a scheduler can never record a stub run as a success.
    raise typer.Exit(code=2)


if __name__ == "__main__":  # pragma: no cover
    app()
