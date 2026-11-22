"""Create campaigns from validated JSON rules and list saved campaigns."""

import math
from datetime import date
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from montaj.cli_support import configured_db
from montaj.db import insert
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
