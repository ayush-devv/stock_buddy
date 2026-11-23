"""Command-line entry point (WP 0.4, 1.4, 1.5).

pi status | pi run [--date D] | pi notify-test
pi secret status|set|delete NAME | pi schedule show|install|uninstall
"""

from __future__ import annotations

import json
from datetime import date

import typer

from pi import pipeline
from pi.alerts.telegram import AlertMessage, TelegramNotifier
from pi.config import CONFIG_FILES, ConfigError, load_one, project_root
from pi.ops import heartbeat, launchd, secrets
from pi.store import db

app = typer.Typer(no_args_is_help=True, add_completion=False)
secret_app = typer.Typer(no_args_is_help=True, help="Secrets in the macOS Keychain.")
schedule_app = typer.Typer(no_args_is_help=True, help="The 19:00 launchd LaunchAgent.")
app.add_typer(secret_app, name="secret")
app.add_typer(schedule_app, name="schedule")


def _notifier() -> TelegramNotifier | None:
    """Telegram notifier from the Keychain, or None if not set up (alerts are then
    recorded as undelivered, never lost)."""
    try:
        token, chat_id = secrets.get("telegram_bot_token"), secrets.get("telegram_chat_id")
    except secrets.SecretError as exc:
        typer.echo(f"warning: {exc}", err=True)
        return None
    if not token or not chat_id:
        return None
    return TelegramNotifier(token, chat_id)


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
        result = pipeline.run(project_root(), force_date=force, notifier=_notifier())
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
    if result.alert:
        typer.echo(f"alert: {result.alert}", err=True)
    if result.exit_code == pipeline.EXIT_OK and force is None:
        # Only a clean scheduled-style run tells the monitor "alive".
        try:
            url = secrets.get("heartbeat_url")
        except secrets.SecretError as exc:
            url = None
            typer.echo(f"warning: {exc}", err=True)
        typer.echo(f"heartbeat: {heartbeat.ping(url).detail}")
    raise typer.Exit(code=result.exit_code)


@app.command("notify-test")
def notify_test() -> None:
    """Send a 🟡 TEST message to your Telegram chat."""
    notifier = _notifier()
    if notifier is None:
        typer.echo("telegram not configured: pi secret set telegram_bot_token / telegram_chat_id")
        raise typer.Exit(code=1)
    try:
        notifier.send(AlertMessage("yellow", "TEST"))
    except RuntimeError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo("sent: 🟡 TEST")


# --------------------------------------------------------------------------- secrets


@secret_app.command("status")
def secret_status() -> None:
    """Which secrets are set (values are never shown)."""
    for name, is_set in secrets.status().items():
        typer.echo(f"  {name:<20} {'set' if is_set else 'missing'}   {secrets.KNOWN[name]}")


@secret_app.command("set")
def secret_set(name: str) -> None:
    """Store a secret in the Keychain. The value is typed at a hidden prompt."""
    if name not in secrets.KNOWN:
        raise typer.BadParameter(f"known: {', '.join(secrets.KNOWN)}", param_hint="NAME")
    value = typer.prompt(secrets.KNOWN[name], hide_input=True)
    if name == "heartbeat_url":
        try:
            value = heartbeat.validate_url(value)
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
    secrets.put(name, value)
    typer.echo(f"{name} stored in the Keychain")


@secret_app.command("delete")
def secret_delete(name: str) -> None:
    """Remove a secret from the Keychain."""
    secrets.delete(name)
    typer.echo(f"{name} removed")


# --------------------------------------------------------------------------- schedule


@schedule_app.command("show")
def schedule_show() -> None:
    """Print the LaunchAgent plist that `install` would write."""
    typer.echo(launchd.render(project_root()).decode())
    typer.echo(f"# would be written to {launchd.plist_path()}")


@schedule_app.command("install")
def schedule_install() -> None:
    """Install and load the 19:00 LaunchAgent (writes to ~/Library/LaunchAgents)."""
    tz = launchd.local_tz_name()
    if tz != "IST":
        typer.echo(f"warning: this Mac's time zone is {tz}; 19:00 is local time, not IST")
    try:
        path = launchd.install(project_root())
    except (FileNotFoundError, RuntimeError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"installed {path}; runs daily at 19:00, on wake if missed, and at login")


@schedule_app.command("uninstall")
def schedule_uninstall() -> None:
    """Unload and remove the LaunchAgent."""
    removed = launchd.uninstall()
    typer.echo("removed" if removed else "was not installed")


if __name__ == "__main__":  # pragma: no cover
    app()
