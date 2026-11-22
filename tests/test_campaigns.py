from pathlib import Path

from typer.testing import CliRunner

from montaj.cli import app
from montaj.db import connect, fetch
from montaj.models import Campaign, CampaignRules


def add_args(rules: Path) -> list[str]:
    return [
        "campaign",
        "add",
        "--name",
        "Example",
        "--platform",
        "youtube",
        "--rate",
        "1",
        "--min-views",
        "10",
        "--cap",
        "20",
        "--deadline",
        "2030-01-01",
        "--rules",
        str(rules),
    ]


def test_add_list_and_rules_round_trip(db_path: Path, tmp_path: Path) -> None:
    rules = CampaignRules(
        min_seconds=20, max_seconds=60, required_hashtags=["#Example"]
    )
    path = tmp_path / "rules.json"
    path.write_text(rules.model_dump_json(), encoding="utf-8")
    runner = CliRunner()

    added = runner.invoke(app, add_args(path))
    assert added.exit_code == 0, added.output
    with connect(db_path) as db:
        campaign = fetch(db, Campaign, 1)
        assert campaign is not None
        assert campaign.rules == rules
    listed = runner.invoke(app, ["campaign", "list"])
    assert listed.exit_code == 0, listed.output
    assert "1\tExample\tyoutube\t2030-01-01" in listed.output


def test_invalid_rules_report_fields_and_prevent_insert(
    db_path: Path, tmp_path: Path
) -> None:
    path = tmp_path / "invalid.json"
    path.write_text(
        '{"min_seconds": 0, "max_seconds": 60, "extra": true}', encoding="utf-8"
    )
    result = CliRunner().invoke(app, add_args(path))
    assert result.exit_code == 2
    assert "min_seconds" in result.output
    assert "extra" in result.output
    with connect(db_path) as db:
        assert fetch(db, Campaign, 1) is None
