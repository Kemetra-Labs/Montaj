from pathlib import Path

import pytest
from typer.testing import CliRunner

from montaj.cli import app
from montaj.config import Settings


def test_cli_help() -> None:
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "Prepare reviewed short clips" in result.output


def test_settings_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPGRAM_API_KEY", "test-deepgram-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("MONTAJ_DATA_DIR", "")
    monkeypatch.setenv("MONTAJ_DEEPGRAM_MODEL", "")
    monkeypatch.setenv("MONTAJ_CLAUDE_MODEL", "test-claude-model")
    monkeypatch.setenv("MONTAJ_FONT_PATH", "fonts/arabic.ttf")
    monkeypatch.setenv("MONTAJ_BACKUP_KEEP", "3")
    monkeypatch.setenv("MONTAJ_MIN_FREE_MB", "2048")
    monkeypatch.setenv("MONTAJ_CLEAN_OLDER_THAN_DAYS", "21")

    settings = Settings.from_env()

    assert settings.data_dir == Path("data")
    assert settings.deepgram_model == "nova-3"
    assert settings.claude_model == "test-claude-model"
    assert settings.font_path == Path("fonts/arabic.ttf")
    assert settings.anthropic_api_key is None
    assert settings.deepgram_api_key is not None
    assert settings.deepgram_api_key.get_secret_value() == "test-deepgram-key"
    assert "test-deepgram-key" not in repr(settings)
    assert settings.backup_keep == 3
    assert settings.min_free_mb == 2048
    assert settings.clean_older_than_days == 21
