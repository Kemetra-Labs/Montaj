import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from montaj import doctor
from montaj.cli import app
from montaj.config import Settings
from montaj.db import connect


def test_doctor_reports_versions_and_redacts_secrets(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPGRAM_API_KEY", "deepgram-secret-value")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret-value")
    monkeypatch.setattr(doctor.shutil, "which", lambda command: f"/test/{command}")

    def version(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, f"{Path(args[0]).name} 1.2.3\n", "")

    monkeypatch.setattr(doctor.subprocess, "run", version)
    result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    for name in ("ffmpeg", "ffprobe", "yt-dlp"):
        assert f"{name}: {name} 1.2.3" in result.output
    for name in ("DEEPGRAM_API_KEY", "ANTHROPIC_API_KEY"):
        assert f"{name}: set" in result.output
    assert "secret-value" not in result.output
    assert "SQLite: schema version" in result.output
    assert Settings().resolved_font_path.name in result.output


def test_doctor_reports_missing_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    invalid_dir = tmp_path / "not-a-directory"
    invalid_dir.touch()
    monkeypatch.setenv("MONTAJ_DATA_DIR", str(invalid_dir))
    monkeypatch.setenv("MONTAJ_FONT_PATH", str(tmp_path / "missing.ttf"))
    monkeypatch.setattr(doctor.shutil, "which", lambda command: None)
    result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 1
    assert result.output.count("FAIL") == 8
    assert "writable directory" in result.output
    assert "montaj db init" in result.output


def test_database_schema_is_checked(db_path: Path) -> None:
    with connect(db_path) as db:
        db.execute("UPDATE schema_version SET version = 999")
    result = {
        check.name: check for check in doctor.checks(Settings(data_dir=db_path.parent))
    }
    assert not result["SQLite"].ok
    assert "Schema 999" in result["SQLite"].detail
