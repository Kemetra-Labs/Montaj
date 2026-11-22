"""Command-line entry point for Montaj."""

import sqlite3

import typer

from montaj.config import Settings
from montaj.costs import cost_totals
from montaj.db import SchemaVersionError, connect, init_db

app = typer.Typer(no_args_is_help=True)
db_app = typer.Typer(no_args_is_help=True)
app.add_typer(db_app, name="db", help="Manage the SQLite database.")


@app.callback()
def main() -> None:
    """Prepare reviewed short clips from permitted videos."""


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
