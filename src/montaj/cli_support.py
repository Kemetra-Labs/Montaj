"""Common local setup for CLI commands."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

import typer

from montaj.config import Settings
from montaj.db import connect


@contextmanager
def configured_db() -> Iterator[sqlite3.Connection]:
    path = Settings.from_env().db_path
    if not path.is_file():
        typer.echo("Database is missing. Run montaj db init first.", err=True)
        raise typer.Exit(1)
    try:
        with connect(path) as db:
            yield db
    except sqlite3.Error as exc:
        typer.echo(f"Database error: {exc}", err=True)
        raise typer.Exit(1) from exc
