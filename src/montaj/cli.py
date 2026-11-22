"""Command-line entry point for Montaj."""

import sqlite3

import typer

from montaj.accounts import account_app
from montaj.campaigns import campaign_app
from montaj.config import Settings
from montaj.costs import cost_totals
from montaj.db import SchemaVersionError, connect, init_db
from montaj.doctor import checks
from montaj.ingest import ingest

app = typer.Typer(no_args_is_help=True)
db_app = typer.Typer(no_args_is_help=True)
app.add_typer(db_app, name="db", help="Manage the SQLite database.")
app.add_typer(campaign_app, name="campaign", help="Manage campaigns and their rules.")
app.add_typer(account_app, name="account", help="Manage posting accounts.")
app.command("ingest")(ingest)


@app.callback()
def main() -> None:
    """Prepare reviewed short clips from permitted videos."""


@app.command("doctor")
def doctor() -> None:
    """Check tools, credentials, and local assets without making network calls."""
    try:
        settings = Settings.from_env()
    except ValueError as exc:
        typer.echo(
            "Invalid environment configuration. Check MONTAJ_* settings.", err=True
        )
        raise typer.Exit(1) from exc
    results = checks(settings)
    for check in results:
        status = "OK" if check.ok else "FAIL"
        typer.echo(f"{status:4}  {check.name}: {check.detail}")
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
        with connect(path) as db:
            totals = cost_totals(db, source)
    except sqlite3.Error as exc:
        typer.echo(f"Could not read costs: {exc}", err=True)
        raise typer.Exit(1) from exc
    for stage, amount in sorted(totals.by_stage.items()):
        typer.echo(f"Stage {stage}: ${amount:.6f}")
    for source_id, amount in totals.by_source.items():
        typer.echo(f"Source {source_id}: ${amount:.6f}")
    typer.echo(f"Estimated total: ${totals.total:.6f}")
    if totals.approved_clips:
        typer.echo(f"Per approved clip: ${totals.total / totals.approved_clips:.6f}")
