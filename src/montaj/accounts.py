"""Add and list the operator's posting accounts."""

import sqlite3
from pathlib import Path
from typing import Annotated

import typer

from montaj.cli_support import configured_db
from montaj.db import insert
from montaj.models import Account, Niche, Platform

account_app = typer.Typer(no_args_is_help=True)


@account_app.command("add")
def add_account(
    platform: Annotated[Platform, typer.Option()],
    handle: Annotated[str, typer.Option()],
    niche: Annotated[Niche, typer.Option()],
    style_notes: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Register a posting account and optional account-specific style notes."""
    normalized_handle = handle.strip().removeprefix("@").casefold()
    if not normalized_handle or any(char.isspace() for char in normalized_handle):
        raise typer.BadParameter(
            "Enter a handle without spaces.", param_hint="--handle"
        )
    notes = None
    if style_notes is not None:
        try:
            notes = style_notes.read_text(encoding="utf-8").strip() or None
        except (OSError, UnicodeError) as exc:
            raise typer.BadParameter(
                "Supply a readable UTF-8 file.", param_hint="--style-notes"
            ) from exc
    with configured_db() as db:
        try:
            account = insert(
                db,
                Account(
                    platform=platform,
                    handle=normalized_handle,
                    niche=niche,
                    style_notes=notes,
                ),
            )
        except sqlite3.IntegrityError as exc:
            if exc.sqlite_errorcode != sqlite3.SQLITE_CONSTRAINT_UNIQUE:
                raise
            raise typer.BadParameter(
                "That handle is already registered on this platform.",
                param_hint="--handle",
            ) from exc
    typer.echo(f"Added account {account.id}: @{account.handle}")


@account_app.command("list")
def list_accounts() -> None:
    """List registered accounts without exposing style notes."""
    with configured_db() as db:
        rows = db.execute(
            "SELECT id, platform, handle, niche FROM accounts ORDER BY id"
        ).fetchall()
    typer.echo("ID\tPlatform\tHandle\tNiche")
    for row in rows:
        typer.echo(f"{row['id']}\t{row['platform']}\t@{row['handle']}\t{row['niche']}")
