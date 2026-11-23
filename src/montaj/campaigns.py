"""Create campaigns from validated JSON rules and list saved campaigns."""

import math
import sqlite3
from datetime import date
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from montaj.cli_support import configured_db
from montaj.config import Settings
from montaj.db import connect, fetch, insert, save_source
from montaj.ingest import SourceError, load_source, require_campaign_source
from montaj.media import MediaError, source_lock
from montaj.models import Campaign, CampaignRules, Platform

campaign_app = typer.Typer(no_args_is_help=True)


def _load_rules(path: Path) -> CampaignRules:
    try:
        return CampaignRules.model_validate_json(path.read_bytes())
    except OSError as exc:
        raise typer.BadParameter(
            f"Cannot read {path.name}.", param_hint="--rules"
        ) from exc
    except ValidationError as exc:
        errors = []
        for error in exc.errors(include_input=False):
            field = ".".join(map(str, error["loc"])) or "rules"
            errors.append(f"{field}: {error['msg']}")
        raise typer.BadParameter("\n".join(errors), param_hint="--rules") from exc


@campaign_app.command("add")
def add_campaign(
    name: Annotated[str, typer.Option()],
    platform: Annotated[Platform, typer.Option()],
    rate: Annotated[float, typer.Option(min=0)],
    min_views: Annotated[int, typer.Option(min=0)],
    cap: Annotated[float, typer.Option(min=0)],
    deadline: Annotated[
        str, typer.Option(help="Campaign deadline in YYYY-MM-DD format.")
    ],
    rules: Annotated[Path, typer.Option()],
) -> None:
    """Add a campaign and store its validated rules."""
    campaign_rules = _load_rules(rules)
    try:
        deadline_value = date.fromisoformat(deadline)
    except ValueError as exc:
        raise typer.BadParameter("Use YYYY-MM-DD.", param_hint="--deadline") from exc
    if not name.strip():
        raise typer.BadParameter("Enter a campaign name.", param_hint="--name")
    if not all(math.isfinite(value) for value in (rate, cap)):
        raise typer.BadParameter("Enter finite values for rate and cap.")
    campaign = Campaign(
        name=name.strip(),
        platform=platform,
        rate_per_1k=rate,
        min_views=min_views,
        cap=cap,
        deadline=deadline_value,
        rules_json=campaign_rules.model_dump_json(),
    )
    with configured_db() as db:
        saved = insert(db, campaign)
    typer.echo(f"Added campaign {saved.id}: {saved.name}")


@campaign_app.command("update-rules")
def update_rules(
    campaign_id: Annotated[int, typer.Argument(min=1)],
    rules: Annotated[Path, typer.Option()],
) -> None:
    """Replace the validated rules and brief for a campaign."""
    updated_rules = _load_rules(rules)
    with configured_db() as db:
        updated = db.execute(
            "UPDATE campaigns SET rules_json = ? WHERE id = ?",
            (updated_rules.model_dump_json(), campaign_id),
        )
        if updated.rowcount != 1:
            raise typer.BadParameter(f"Campaign {campaign_id} does not exist.")
    typer.echo(f"Updated rules for campaign {campaign_id}.")


@campaign_app.command("link-source")
def link_source(
    source_id: Annotated[int, typer.Argument(min=1)],
    campaign_id: Annotated[int, typer.Option("--campaign", min=1)],
    wait: Annotated[
        float,
        typer.Option("--wait", min=0, help="Seconds to wait if this source is locked."),
    ] = 0,
) -> None:
    """Link an ingested source to a campaign using its existing media and transcript."""
    settings = Settings.from_env()
    try:
        _, _, directory = load_source(settings, source_id)
        with source_lock(directory, command="campaign link-source", wait=wait):
            source, _, _ = load_source(settings, source_id)
            with connect(settings.db_path) as db:
                db.execute("BEGIN IMMEDIATE")
                campaign = fetch(db, Campaign, campaign_id)
                if campaign is None:
                    raise SourceError(f"Campaign {campaign_id} does not exist.", code=2)
                require_campaign_source(source.url or source.path or "", campaign.rules)
                source.campaign_id = campaign_id
                save_source(db, source)
    except SourceError as exc:
        typer.echo(f"Link failed: {exc}", err=True)
        raise typer.Exit(exc.code) from exc
    except (MediaError, OSError, ValueError, sqlite3.Error) as exc:
        typer.echo(f"Link failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"Linked source {source_id} to campaign {campaign_id}.")


@campaign_app.command("list")
def list_campaigns() -> None:
    """List campaign IDs, names, platforms, and deadlines."""
    with configured_db() as db:
        rows = db.execute(
            "SELECT id, name, platform, deadline FROM campaigns ORDER BY id"
        ).fetchall()
    typer.echo("ID\tName\tPlatform\tDeadline")
    for row in rows:
        typer.echo(
            f"{row['id']}\t{row['name']}\t{row['platform']}\t{row['deadline'] or '-'}"
        )
