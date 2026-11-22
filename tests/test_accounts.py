from pathlib import Path

import pytest
from typer.testing import CliRunner

from montaj.cli import app
from montaj.db import connect, fetch
from montaj.models import Account


def add_args(
    platform: str = "youtube", handle: str = "@Test", niche: str = "business"
) -> list[str]:
    return [
        "account",
        "add",
        "--platform",
        platform,
        "--handle",
        handle,
        "--niche",
        niche,
    ]


def test_add_list_and_duplicate(db_path: Path, tmp_path: Path) -> None:
    notes = tmp_path / "style.txt"
    notes.write_text("A private style note", encoding="utf-8")
    runner = CliRunner()
    first = runner.invoke(app, add_args() + ["--style-notes", str(notes)])
    assert first.exit_code == 0, first.output
    duplicate = runner.invoke(app, add_args(handle="test"))
    assert duplicate.exit_code == 2
    assert "already registered" in duplicate.output
    other = runner.invoke(app, add_args(platform="instagram"))
    assert other.exit_code == 0, other.output
    listed = runner.invoke(app, ["account", "list"])
    assert "1\tyoutube\t@test\tbusiness" in listed.output
    assert "private style note" not in listed.output
    with connect(db_path) as db:
        account = fetch(db, Account, 1)
        assert account is not None and account.style_notes == "A private style note"


@pytest.mark.parametrize(
    "platform,niche", [("unknown", "health"), ("youtube", "unknown")]
)
def test_invalid_platform_or_niche(db_path: Path, platform: str, niche: str) -> None:
    result = CliRunner().invoke(app, add_args(platform=platform, niche=niche))
    assert result.exit_code == 2
    with connect(db_path) as db:
        assert fetch(db, Account, 1) is None
