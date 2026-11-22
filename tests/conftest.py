import os
import shutil
import socket
from pathlib import Path

import pytest

from montaj.db import init_db


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
def ffmpeg() -> str:
    executable = shutil.which("ffmpeg")
    if executable is None:
        message = "FFmpeg is required for media tests. Install it and add it to PATH."
        if os.getenv("MONTAJ_REQUIRE_FFMPEG") == "1":
            pytest.fail(message)
        pytest.skip(message)
    return executable
