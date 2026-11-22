"""Command-line entry point for Montaj."""

import sqlite3

import typer

from montaj.config import Settings
from montaj.db import SchemaVersionError, init_db

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
