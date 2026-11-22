"""Offline checks for dependencies and local Montaj configuration."""

import shutil
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryFile

from montaj.config import Settings
from montaj.db import SCHEMA_VERSION, connect


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    ok: bool
    detail: str


def _tool(name: str, version_arg: str) -> Check:
    executable = shutil.which(name)
    if executable is None:
        return Check(name, False, f"Install {name} and add it to PATH.")
    try:
        result = subprocess.run(
            [executable, version_arg],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return Check(name, False, f"Check that {name} runs and reports its version.")
    lines = result.stdout.splitlines()
    return Check(name, True, lines[0] if lines else "available")


def _data_directory(path: Path) -> Check:
    try:
        path.mkdir(parents=True, exist_ok=True)
        with TemporaryFile(dir=path) as probe:
            probe.write(b"montaj")
    except OSError:
        return Check(
            "data directory", False, "Set MONTAJ_DATA_DIR to a writable directory."
        )
    return Check("data directory", True, str(path))


def _database(path: Path) -> Check:
    hint = "Run montaj db init to create or migrate the database."
    if not path.is_file():
        return Check("SQLite", False, hint)
    try:
        with connect(path) as db:
            row = db.execute(
                "SELECT version FROM schema_version WHERE id = 1"
            ).fetchone()
    except sqlite3.Error:
        return Check("SQLite", False, hint)
    if row is None or row[0] != SCHEMA_VERSION:
        found = row[0] if row is not None else "missing"
        return Check(
            "SQLite", False, f"Schema {found}; expected {SCHEMA_VERSION}. {hint}"
        )
    return Check("SQLite", True, f"schema version {SCHEMA_VERSION}")


def checks(settings: Settings) -> list[Check]:
    font = settings.resolved_font_path
    font_exists = font.is_file()
    results = [
        _tool("ffmpeg", "-version"),
        _tool("ffprobe", "-version"),
        _tool("yt-dlp", "--version"),
    ]
    for name, secret in (
        ("DEEPGRAM_API_KEY", settings.deepgram_api_key),
        ("ANTHROPIC_API_KEY", settings.anthropic_api_key),
    ):
        present = bool(secret and secret.get_secret_value().strip())
        results.append(
            Check(
                name,
                present,
                "set" if present else f"missing. Set {name} in the environment.",
            )
        )
    results.extend(
        (
            _data_directory(settings.data_dir),
            _database(settings.db_path),
            Check(
                "Arabic font",
                font_exists,
                str(font)
                if font_exists
                else "Set MONTAJ_FONT_PATH to an Arabic-capable font file.",
            ),
        )
    )
    return results
