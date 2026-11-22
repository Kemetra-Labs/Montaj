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

    settings = Settings.from_env()

    assert settings.data_dir == Path("data")
    assert settings.deepgram_model == "nova-3"
    assert settings.claude_model == "test-claude-model"
    assert settings.font_path == Path("fonts/arabic.ttf")
    assert settings.anthropic_api_key is None
    assert settings.deepgram_api_key is not None
    assert settings.deepgram_api_key.get_secret_value() == "test-deepgram-key"
    assert "test-deepgram-key" not in repr(settings)
