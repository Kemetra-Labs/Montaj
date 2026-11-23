import json
import shutil
import subprocess
import wave
from functools import partial
from http.server import SimpleHTTPRequestHandler
from pathlib import Path
from socketserver import TCPServer
from threading import Thread

import pytest
from typer.testing import CliRunner

from montaj.cli import app
from montaj.config import Settings
from montaj.db import connect, fetch
from montaj.ingest import SourceError, YtDlpDownloader, ingest_video, load_source
from montaj.models import Niche, Source
from tests.fakes import FakeDownloader


@pytest.mark.parametrize(
    ("detail", "message"),
    [
        (
            "No supported JavaScript runtime could be found.\nRequested format is not available",
            "Deno",
        ),
        ("Remote components challenge solver script (github) were skipped", "EJS"),
        ("Requested format is not available", "No usable video and audio format"),
        ("Connection reset by peer", "network"),
        ("Sign in to confirm your age", "age-restricted"),
        ("Got error: fragment 3 not found", "incomplete"),
    ],
)
def test_download_diagnostics_are_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, detail: str, message: str
) -> None:
    url = "https://example.test/video?token=secret-url-value"

    def fail(args: list[str], **kwargs: object) -> None:
        raise subprocess.CalledProcessError(
            1,
            args,
            stderr=f"{detail}\n{url}\nCookie: secret-cookie-value\nAuthorization: Bearer secret-auth-value".encode(),
        )

    monkeypatch.setattr(shutil, "which", lambda name: name)
    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(SourceError, match=message) as caught:
        YtDlpDownloader().download(url, tmp_path / "download")
    assert caught.value.code == 4
    assert "secret-" not in str(caught.value)
    assert "secret-" not in str(caught.value.__cause__)


def test_downloader_accepts_merged_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "download"
    merged = destination.with_suffix(".mkv")

    def download(
        args: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        assert args[args.index("--format") + 1] == "bv*+ba/b"
        assert "--abort-on-unavailable-fragments" in args
        assert "--print" in args and "after_move:filepath" in args
        assert "--no-simulate" in args
        merged.write_bytes(b"merged")
        return subprocess.CompletedProcess(args, 0, f"{merged}\n".encode(), b"")

    monkeypatch.setattr(shutil, "which", lambda name: name)
    monkeypatch.setattr(subprocess, "run", download)
    YtDlpDownloader().download("https://example.test/video", destination)
    assert destination.read_bytes() == b"merged"


def test_download_timeout_omits_command_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def timeout(args: list[str], **kwargs: object) -> None:
        raise subprocess.TimeoutExpired(args, 1800)

    monkeypatch.setattr(shutil, "which", lambda name: name)
    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(SourceError, match="Check connectivity and retry") as caught:
        YtDlpDownloader().download(
            "https://example.test/video?token=secret-url-value", tmp_path / "download"
        )
    assert "secret-url-value" not in str(caught.value)


def test_download_without_completed_output_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: name)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, b"", b""),
    )
    with pytest.raises(SourceError, match="incomplete"):
        YtDlpDownloader().download("https://example.test/video", tmp_path / "download")


def test_refuses_unpermitted_source_before_work(db_path: Path) -> None:
    result = CliRunner().invoke(app, ["ingest", "missing.mp4", "--niche", "games"])
    assert result.exit_code == 1
    assert "Confirm permission with --allowed" in result.output
    assert not (db_path.parent / "sources").exists()
    with connect(db_path) as db:
        assert fetch(db, Source, 1) is None


def test_url_permission_is_checked_before_downloader(
    db_path: Path, media_fixture_dir: Path
) -> None:
    downloader = FakeDownloader(media_fixture_dir / "sample.mp4")
    with pytest.raises(SourceError, match="--basis"):
        ingest_video(
            Settings(data_dir=db_path.parent),
            "https://example.test/media",
            Niche.GAMES,
            allowed=True,
            downloader=downloader,
        )
    assert downloader.urls == []
    assert not (db_path.parent / "sources").exists()


def test_real_selector_accepts_audio_and_video_only_formats(tmp_path: Path) -> None:
    yt_dlp = shutil.which("yt-dlp")
    if yt_dlp is None:
        pytest.skip(
            "Install the pinned yt-dlp release to exercise its selector offline."
        )
    info = tmp_path / "split.json"
    info.write_text(
        json.dumps(
            {
                "id": "split",
                "title": "Split streams",
                "extractor": "generic",
                "extractor_key": "Generic",
                "formats": [
                    {
                        "format_id": "video",
                        "ext": "mp4",
                        "vcodec": "avc1",
                        "acodec": "none",
                        "url": "https://example.invalid/video.mp4",
                    },
                    {
                        "format_id": "audio",
                        "ext": "m4a",
                        "vcodec": "none",
                        "acodec": "mp4a",
                        "url": "https://example.invalid/audio.m4a",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    command = [
        yt_dlp,
        "--ignore-config",
        "--no-plugin-dirs",
        "--simulate",
        "--load-info-json",
        str(info),
        "--print",
        "format_id",
        "--format",
    ]
    original = subprocess.run(
        [*command, "best[ext=mp4]/best"], capture_output=True, text=True, timeout=10
    )
    assert original.returncode != 0
    assert "Requested format is not available" in original.stderr
    improved = subprocess.run(
        [*command, "bv*+ba/b"], capture_output=True, text=True, timeout=10, check=True
    )
    assert improved.stdout.strip() == "video+audio"


@pytest.mark.media
def test_real_download_merge_and_normalization(
    ffmpeg: str,
    db_path: Path,
    media_fixture_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if shutil.which("yt-dlp") is None or shutil.which("ffprobe") is None:
        pytest.skip(
            "Install yt-dlp and FFprobe for the offline download and merge test."
        )
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-i",
            str(media_fixture_dir / "sample.mp4"),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-c",
            "copy",
            "-f",
            "dash",
            "sample.mpd",
        ],
        check=True,
        timeout=10,
        # ffmpeg writes DASH segments relative to its working directory on
        # Windows when the manifest path is absolute, so run it in tmp_path.
        cwd=tmp_path,
    )
    settings = Settings(data_dir=db_path.parent)
    handler = partial(SimpleHTTPRequestHandler, directory=tmp_path)
    with TCPServer(("127.0.0.1", 0), handler) as server:
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
        thread.start()
        try:
            source_id, reused = ingest_video(
                settings,
                f"http://127.0.0.1:{server.server_address[1]}/sample.mpd",
                Niche.GAMES,
                allowed=True,
                basis="repository fixture",
            )
        finally:
            server.shutdown()
            thread.join()
    assert not reused
    _, metadata, directory = load_source(settings, source_id)
    assert metadata.duration == pytest.approx(18, abs=0.25)
    assert (directory / "video.mp4").stat().st_size > 0
    with wave.open(str(directory / "audio.wav"), "rb") as audio:
        assert (audio.getframerate(), audio.getnchannels()) == (16_000, 1)
        assert audio.getnframes() > 0


@pytest.mark.media
def test_incomplete_and_silent_downloads_can_retry(
    ffmpeg: str, db_path: Path, media_fixture_dir: Path, tmp_path: Path
) -> None:
    if shutil.which("ffprobe") is None:
        pytest.skip("Install FFprobe alongside FFmpeg to run ingest tests.")
    settings = Settings(data_dir=db_path.parent)
    source = media_fixture_dir / "sample.mp4"
    silent = tmp_path / "silent.mp4"
    subprocess.run(
        [ffmpeg, "-v", "error", "-i", str(source), "-an", "-c:v", "copy", str(silent)],
        check=True,
        timeout=10,
    )
    broken = tmp_path / "partial.mp4"
    broken.write_bytes(source.read_bytes()[:256])

    def ingest(path: Path) -> tuple[int, bool]:
        return ingest_video(
            settings,
            "https://example.test/media",
            Niche.GAMES,
            allowed=True,
            basis="campaign permission",
            downloader=FakeDownloader(path),
        )

    directory = db_path.parent / "sources/1"
    for path, message in (
        (broken, "corrupt or incomplete"),
        (silent, "no audio stream"),
    ):
        with pytest.raises(SourceError, match=message) as caught:
            ingest(path)
        assert caught.value.code == 5
        assert {item.name for item in directory.iterdir()} == {".lock"}
        with connect(db_path) as db:
            row = fetch(db, Source, 1)
            assert row and row.failure_note and message in row.failure_note

    assert ingest(source) == (1, False)
    row, _, _ = load_source(settings, 1)
    assert row.failure_note is None
    assert ingest(source) == (1, True)


@pytest.mark.media
@pytest.mark.parametrize(
    "limit", [{"max_source_minutes": 0.1}, {"max_source_mb": 0.01}]
)
def test_download_limits_are_enforced_before_normalization(
    ffmpeg: str,
    db_path: Path,
    media_fixture_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit: dict[str, float],
) -> None:
    if shutil.which("ffprobe") is None:
        pytest.skip("Install FFprobe alongside FFmpeg to run ingest tests.")

    def forbid(*args: object) -> None:
        pytest.fail("Over-limit media must be rejected before normalization.")

    monkeypatch.setattr("montaj.ingest._prepare_video", forbid)
    settings = Settings(data_dir=db_path.parent, **limit)
    with pytest.raises(SourceError, match="limit") as caught:
        ingest_video(
            settings,
            "https://example.test/media",
            Niche.GAMES,
            allowed=True,
            basis="campaign permission",
            downloader=FakeDownloader(media_fixture_dir / "sample.mp4"),
        )
    assert caught.value.code == 3
    assert not (db_path.parent / "sources/1/meta.json").exists()


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
        settings,
        url,
        Niche.GAMES,
        allowed=True,
        basis="campaign allowed-source list",
        downloader=downloader,
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
        settings,
        url,
        Niche.GAMES,
        allowed=True,
        basis="campaign allowed-source list",
        downloader=downloader,
    ) == (source_id, True)
    assert downloader.urls == [url]

    with connect(db_path) as db:
        db.execute("UPDATE sources SET source_allowed = 0 WHERE id = ?", (source_id,))
    with pytest.raises(SourceError, match="lacks permission"):
        ingest_video(
            settings,
            url,
            Niche.GAMES,
            allowed=True,
            basis="campaign allowed-source list",
            downloader=downloader,
        )


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
        settings,
        str(original),
        Niche.GAMES,
        allowed=True,
        basis="creator written permission",
    )
    assert not reused
    assert ingest_video(
        settings,
        str(alias),
        Niche.GAMES,
        allowed=True,
        basis="creator written permission",
    ) == (
        initial_id,
        True,
    )
