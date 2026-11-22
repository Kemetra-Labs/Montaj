import os
import shutil
import socket
from pathlib import Path

import pytest

from montaj.config import Settings
from montaj.db import connect, init_db, insert
from montaj.models import Niche, Source


@pytest.fixture(autouse=True)
def offline_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    def block_network(*args: object, **kwargs: object) -> None:
        pytest.fail("Tests must inject a fake transport for network operations.")

    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(socket.socket, "connect", block_network)
    monkeypatch.setattr(socket.socket, "connect_ex", block_network)
    monkeypatch.setattr(socket, "getaddrinfo", block_network)


@pytest.fixture
def data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    directory = tmp_path / "data"
    directory.mkdir()
    monkeypatch.setenv("MONTAJ_DATA_DIR", str(directory))
    return directory


@pytest.fixture
def db_path(data_dir: Path) -> Path:
    path = data_dir / "montaj.db"
    init_db(path)
    return path


@pytest.fixture
def media_fixture_dir() -> Path:
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def prepared_source(db_path: Path, media_fixture_dir: Path) -> Settings:
    settings = Settings(data_dir=db_path.parent)
    with connect(db_path) as db:
        insert(
            db,
            Source(
                id=1,
                path="source.mp4",
                title="Fixture",
                niche=Niche.GAMES,
                source_allowed=True,
            ),
        )
    directory = db_path.parent / "sources/1"
    directory.mkdir(parents=True)
    shutil.copyfile(media_fixture_dir / "sample.mp4", directory / "video.mp4")
    shutil.copyfile(media_fixture_dir / "sample.wav", directory / "audio.wav")
    (directory / "meta.json").write_text(
        '{"title":"Fixture","duration":18,"video_hash":"video","audio_hash":"audio"}',
        encoding="utf-8",
    )
    return settings


@pytest.fixture
def ffmpeg() -> str:
    executable = shutil.which("ffmpeg")
    if executable is None:
        message = "FFmpeg is required for media tests. Install it and add it to PATH."
        if os.getenv("MONTAJ_REQUIRE_FFMPEG") == "1":
            pytest.fail(message)
        pytest.skip(message)
    return executable
