"""Command-line entry point for Montaj."""

import sqlite3

import typer

from montaj.accounts import account_app
from montaj.campaigns import campaign_app
from montaj.candidates import candidates
from montaj.captions import captions
from montaj.check import check as check_clips
from montaj.config import Settings
from montaj.costs import AttemptTotals, cost_totals
from montaj.cut import cut
from montaj.db import SchemaVersionError, connect, init_db
from montaj.doctor import Check, checks, online_checks
from montaj.draft import draft
from montaj.export import export_clips
from montaj.housekeeping import HousekeepingError, backup_database, clean_data
from montaj.ingest import ingest
from montaj.log import configure_logging
from montaj.post_log import post_log_app
from montaj.preview import dashboard, preview_app
from montaj.rank import rank
from montaj.review import review
from montaj.run import run
from montaj.transcribe import transcribe

app = typer.Typer(no_args_is_help=True)
db_app = typer.Typer(no_args_is_help=True)
app.add_typer(db_app, name="db", help="Manage the SQLite database.")
app.add_typer(campaign_app, name="campaign", help="Manage campaigns and their rules.")
app.add_typer(account_app, name="account", help="Manage posting accounts.")
app.add_typer(preview_app, name="preview")
app.add_typer(
    post_log_app, name="post-log", help="Record posts that were published by hand."
)
app.command("ingest")(ingest)
app.command("transcribe")(transcribe)
app.command("candidates")(candidates)
app.command("rank")(rank)
app.command("cut")(cut)
app.command("captions")(captions)
app.command("draft")(draft)
app.command("check")(check_clips)
app.command("run")(run)
app.command("review")(review)
app.command("export")(export_clips)
app.command("dashboard")(dashboard)


@app.callback()
def main() -> None:
    """Prepare reviewed short clips from permitted videos."""
    try:
        configure_logging(Settings.from_env())
    except ValueError:
        return


def _status(check: Check) -> str:
    if not check.ok:
        return "FAIL"
    if check.warning:
        return "WARN"
    return "OK"


@app.command("doctor")
def doctor(
    online: bool = typer.Option(
        False, "--online", help="Send one tiny request to Deepgram and one to Claude."
    ),
) -> None:
    """Check tools, credentials, and local assets. Add --online to probe the APIs."""
    try:
        settings = Settings.from_env()
    except ValueError as exc:
        typer.echo(
            "Invalid environment configuration. Check MONTAJ_* settings.", err=True
        )
        raise typer.Exit(1) from exc
    results = checks(settings)
    if online:
        probed, cost = online_checks(settings)
        results.extend(probed)
    for check in results:
        typer.echo(f"{_status(check):4}  {check.name}: {check.detail}")
    if online:
        typer.echo(f"Smoke test: ${cost:.6f}")
    if any(not check.ok for check in results):
        raise typer.Exit(1)


@db_app.command("init")
def initialize_database() -> None:
    """Create the Montaj database."""
    path = Settings.from_env().db_path
    try:
        init_db(path)
    except (OSError, sqlite3.Error, SchemaVersionError) as exc:
        typer.echo(f"Could not initialize the database: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"Database ready at {path}")


@app.command("costs")
def show_costs(source: int | None = typer.Option(None, min=1)) -> None:
    """Report estimated API costs by stage and source."""
    path = Settings.from_env().db_path
    if not path.is_file():
        typer.echo("Database is missing. Run montaj db init first.", err=True)
        raise typer.Exit(1)
    try:
        with connect(path, read_only=True) as db:
            totals = cost_totals(db, source)
    except sqlite3.Error as exc:
        typer.echo(f"Could not read costs: {exc}", err=True)
        raise typer.Exit(1) from exc
    for stage, amount in sorted(totals.by_stage.items()):
        typer.echo(f"Stage {stage}: ${amount:.6f}")
    for source_id, amount in totals.by_source.items():
        typer.echo(f"Source {source_id}: ${amount:.6f}")
    typer.echo(f"Estimated total: ${totals.total:.6f}")
    for outcome, label in (
        ("success", "Successful calls"),
        ("failure", "Failed attempts"),
        ("reserved", "Unsettled reservations"),
    ):
        attempts = totals.by_outcome.get(outcome, AttemptTotals())
        typer.echo(f"{label}: {attempts.count} (${attempts.estimate:.6f} estimated)")
    typer.echo("Cached work creates no API attempt. See stage output for reuse.")
    if totals.approved_clips:
        typer.echo(f"Per approved clip: ${totals.total / totals.approved_clips:.6f}")


@app.command("backup")
def backup_command() -> None:
    """Copy the database into data/backups and keep the newest copies."""
    try:
        settings = Settings.from_env()
    except ValueError as exc:
        typer.echo(
            "Invalid environment configuration. Check MONTAJ_* settings.", err=True
        )
        raise typer.Exit(1) from exc
    try:
        path = backup_database(settings)
    except (HousekeepingError, OSError, sqlite3.Error) as exc:
        typer.echo(f"Backup failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"Backup written to {path}")


@app.command("clean")
def clean_command(
    older_than_days: int | None = typer.Option(
        None, "--older-than-days", min=0, help="Only files older than this many days."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print what would be removed and delete nothing."
    ),
) -> None:
    """Delete old source videos and raw renders that finished clips no longer need."""
    try:
        settings = Settings.from_env()
    except ValueError as exc:
        typer.echo(
            "Invalid environment configuration. Check MONTAJ_* settings.", err=True
        )
        raise typer.Exit(1) from exc
    try:
        typer.echo(
            clean_data(settings, older_than_days=older_than_days, dry_run=dry_run)
        )
    except (HousekeepingError, OSError, sqlite3.Error) as exc:
        typer.echo(f"Clean failed: {exc}", err=True)
        raise typer.Exit(1) from exc
