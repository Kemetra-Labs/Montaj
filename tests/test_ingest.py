import shutil
import wave
from pathlib import Path

import pytest
from typer.testing import CliRunner

from montaj.cli import app
from montaj.config import Settings
from montaj.db import connect, fetch
from montaj.ingest import SourceError, ingest_video, load_source
from montaj.models import Niche, Source
from tests.fakes import FakeDownloader


def test_refuses_unpermitted_source_before_work(db_path: Path) -> None:
    result = CliRunner().invoke(app, ["ingest", "missing.mp4", "--niche", "games"])
    assert result.exit_code == 1
    assert "Confirm permission with --allowed" in result.output
    assert not (db_path.parent / "sources").exists()
    with connect(db_path) as db:
        assert fetch(db, Source, 1) is None


@pytest.mark.media
def test_import_artifacts_and_reuse_url(
    ffmpeg: str, db_path: Path, media_fixture_dir: Path
) -> None:
    if shutil.which("ffprobe") is None:
        pytest.skip("Install FFprobe alongside FFmpeg to run ingest tests.")
    settings = Settings(data_dir=db_path.parent)
    downloader = FakeDownloader(media_fixture_dir / "sample.mp4")
    url = "https://example.test/media"
    source_id, reused = ingest_video(
        settings, url, Niche.GAMES, allowed=True, downloader=downloader
    )
    assert not reused
    source, metadata, path = load_source(settings, source_id)
    assert source.source_allowed
    assert metadata.duration == pytest.approx(18, abs=0.25)
    assert metadata.url == url
    assert (path / "video.mp4").is_file()
    with wave.open(str(path / "audio.wav"), "rb") as audio:
        assert (audio.getframerate(), audio.getnchannels()) == (16_000, 1)
    assert ingest_video(
        settings, url, Niche.GAMES, allowed=True, downloader=downloader
    ) == (source_id, True)
    assert downloader.urls == [url]

    with connect(db_path) as db:
        db.execute("UPDATE sources SET source_allowed = 0 WHERE id = ?", (source_id,))
    with pytest.raises(SourceError, match="lacks permission"):
        ingest_video(settings, url, Niche.GAMES, allowed=True, downloader=downloader)


@pytest.mark.media
def test_identical_local_files_share_source_id(
    ffmpeg: str, db_path: Path, media_fixture_dir: Path, tmp_path: Path
) -> None:
    if shutil.which("ffprobe") is None:
        pytest.skip("Install FFprobe alongside FFmpeg to run ingest tests.")
    original = media_fixture_dir / "sample.mp4"
    alias = tmp_path / "same.mp4"
    shutil.copyfile(original, alias)
    settings = Settings(data_dir=db_path.parent)
    initial_id, reused = ingest_video(
        settings, str(original), Niche.GAMES, allowed=True
    )
    assert not reused
    assert ingest_video(settings, str(alias), Niche.GAMES, allowed=True) == (
        initial_id,
        True,
    )
