import logging
import os
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import SecretStr
from typer.testing import CliRunner

from montaj.cli import app
from montaj.config import Settings
from montaj.db import connect, insert
from montaj.doctor import YT_DLP_EJS_PIN, YT_DLP_PIN, checks
from montaj.housekeeping import (
    HousekeepingError,
    backup_database,
    clean_data,
    plan_cleanup,
)
from montaj.ingest import SourceError, ingest_video
from montaj.log import configure_logging
from montaj.models import Clip, ClipStatus, Niche, Source

NOW = datetime(2026, 6, 15, tzinfo=UTC)


def _source(db_path: Path, source_id: int) -> None:
    with connect(db_path) as db:
        insert(
            db,
            Source(
                id=source_id,
                path=f"source-{source_id}.mp4",
                title=f"Source {source_id}",
                niche=Niche.GAMES,
                source_allowed=True,
            ),
        )


def _clip(db_path: Path, clip_id: int, source_id: int, status: ClipStatus) -> None:
    with connect(db_path) as db:
        insert(
            db,
            Clip(
                id=clip_id,
                source_id=source_id,
                start_s=0,
                end_s=1,
                status=status,
            ),
        )


def _age(path: Path, days_old: int, payload: bytes = b"0123456789abcdef") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    stamp = (NOW - timedelta(days=days_old)).timestamp()
    os.utime(path, (stamp, stamp))


def test_backup_is_restorable_and_keeps_the_newest_copies(db_path: Path) -> None:
    settings = Settings(data_dir=db_path.parent, backup_keep=2)
    with connect(db_path) as db:
        insert(
            db,
            Source(
                id=1,
                path="keep.mp4",
                title="Keep me",
                niche=Niche.GAMES,
                source_allowed=True,
            ),
        )
    copies = [
        backup_database(settings, now=datetime(2026, 1, day, tzinfo=UTC))
        for day in (1, 2, 3)
    ]
    remaining = sorted((settings.data_dir / "backups").glob("montaj-*.db"))
    assert copies[0] not in remaining
    assert remaining == copies[1:]
    restored = sqlite3.connect(remaining[-1])
    try:
        assert restored.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        title = restored.execute("SELECT title FROM sources WHERE id = 1").fetchone()
    finally:
        restored.close()
    assert title == ("Keep me",)
    with connect(db_path) as db:
        db.execute("UPDATE sources SET title = 'Changed after backup' WHERE id = 1")
    untouched = sqlite3.connect(remaining[-1])
    try:
        still = untouched.execute("SELECT title FROM sources WHERE id = 1").fetchone()
    finally:
        untouched.close()
    assert still == ("Keep me",)


def test_backup_refuses_a_missing_database(tmp_path: Path) -> None:
    with pytest.raises(HousekeepingError, match="missing"):
        backup_database(Settings(data_dir=tmp_path))


def test_backup_command_writes_a_copy(db_path: Path) -> None:
    result = CliRunner().invoke(app, ["backup"])
    assert result.exit_code == 0, result.output
    written = list((db_path.parent / "backups").glob("montaj-*.db"))
    assert len(written) == 1
    assert str(written[0]) in result.output


def test_clean_drops_old_finished_media_and_keeps_the_rest(db_path: Path) -> None:
    root = db_path.parent
    _source(db_path, 1)
    _clip(db_path, 1, 1, ClipStatus.RANKED)
    _source(db_path, 2)
    _clip(db_path, 2, 2, ClipStatus.POSTED)
    _source(db_path, 3)
    _source(db_path, 4)
    _clip(db_path, 4, 4, ClipStatus.REJECTED)
    _source(db_path, 5)
    _clip(db_path, 5, 5, ClipStatus.EXPORTED)
    _source(db_path, 6)
    _clip(db_path, 6, 6, ClipStatus.POSTED)
    _age(root / "sources/1/video.mp4", 30)
    _age(root / "sources/2/video.mp4", 30)
    _age(root / "sources/2/transcript.json", 30, b'{"words":[]}')
    _age(root / "sources/3/video.mp4", 30)
    _age(root / "sources/4/video.mp4", 30)
    _age(root / "sources/5/video.mp4", 30)
    _age(root / "sources/6/video.mp4", 1)
    _age(root / "clips/2/raw.mp4", 30)
    _age(root / "clips/2/final.mp4", 30)
    _age(root / "clips/4/raw.mp4", 30)
    _age(root / "clips/5/raw.mp4", 30)
    _age(root / "clips/6/raw.mp4", 1)
    exported = root / "exports/2026-06-01/1-handle/01_clip.mp4"
    _age(exported, 30, b"exported-bytes")
    settings = Settings(data_dir=root, clean_older_than_days=14)
    report = clean_data(settings, dry_run=True, now=NOW)
    assert "Would remove" in report
    assert "Would free" in report
    assert (root / "sources/2/video.mp4").is_file()
    removed = plan_cleanup(settings, older_than_days=14, now=NOW)
    assert {item.path.name for item in removed} == {"video.mp4", "raw.mp4"}
    assert sum(item.size for item in removed) == 16 * 4
    applied = clean_data(settings, dry_run=False, now=NOW)
    assert "Removed" in applied
    assert "Freed 64 bytes." in applied
    for kept in (
        root / "sources/1/video.mp4",
        root / "sources/2/transcript.json",
        root / "sources/3/video.mp4",
        root / "sources/5/video.mp4",
        root / "sources/6/video.mp4",
        root / "clips/2/final.mp4",
        root / "clips/5/raw.mp4",
        root / "clips/6/raw.mp4",
        exported,
        db_path,
    ):
        assert kept.is_file(), kept
    for gone in (
        root / "sources/2/video.mp4",
        root / "sources/4/video.mp4",
        root / "clips/2/raw.mp4",
        root / "clips/4/raw.mp4",
    ):
        assert not gone.exists(), gone


def test_clean_command_dry_run_deletes_nothing(db_path: Path) -> None:
    _source(db_path, 1)
    _clip(db_path, 1, 1, ClipStatus.POSTED)
    video = db_path.parent / "sources/1/video.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"old-source-video")
    stamp = time.time() - 30 * 86400
    os.utime(video, (stamp, stamp))
    result = CliRunner().invoke(app, ["clean", "--dry-run", "--older-than-days", "14"])
    assert result.exit_code == 0, result.output
    assert "Would remove" in result.output
    assert "Would free" in result.output
    assert video.is_file()


def test_clean_refuses_without_a_database(tmp_path: Path) -> None:
    with pytest.raises(HousekeepingError, match="missing"):
        clean_data(Settings(data_dir=tmp_path), dry_run=True)


def test_disk_warning_uses_a_fake_free_space(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("montaj.doctor.shutil.which", lambda command: None)
    low = {
        item.name: item
        for item in checks(
            Settings(data_dir=tmp_path, min_free_mb=100),
            free_bytes=lambda path: 10 * 1024 * 1024,
        )
    }
    assert low["disk space"].ok and low["disk space"].warning
    assert "10 MB free" in low["disk space"].detail
    high = {
        item.name: item
        for item in checks(
            Settings(data_dir=tmp_path, min_free_mb=100),
            free_bytes=lambda path: 500 * 1024 * 1024,
        )
    }
    assert high["disk space"].ok and not high["disk space"].warning


def test_ingest_refuses_when_the_source_would_not_fit(
    db_path: Path, tmp_path: Path
) -> None:
    settings = Settings(data_dir=db_path.parent, min_free_mb=100)
    video = tmp_path / "wide.mp4"
    video.write_bytes(b"x" * 50)

    class Boom:
        def download(self, url: str, destination: Path) -> None:
            raise AssertionError(url)

    with pytest.raises(SourceError, match="0.0 MB is free") as local:
        ingest_video(
            settings,
            str(video),
            Niche.GAMES,
            allowed=True,
            basis="creator written permission",
            free_bytes=lambda path: 10,
        )
    assert local.value.code == 3
    assert not (db_path.parent / "sources/1/video.mp4").exists()
    with pytest.raises(SourceError, match="need 100 MB") as remote:
        ingest_video(
            settings,
            "https://example.test/video",
            Niche.GAMES,
            allowed=True,
            basis="campaign allowed-source list",
            downloader=Boom(),
            free_bytes=lambda path: 1024,
        )
    assert remote.value.code == 3


def test_reused_source_does_not_need_free_space(db_path: Path) -> None:
    settings = Settings(data_dir=db_path.parent, min_free_mb=100)
    directory = settings.data_dir / "sources" / "1"
    directory.mkdir(parents=True)
    (directory / "video.mp4").write_bytes(b"video")
    (directory / "audio.wav").write_bytes(b"audio")
    (directory / "meta.json").write_text(
        '{"title":"Fixture","duration":18,"video_hash":"v","audio_hash":"a"}\n',
        encoding="utf-8",
    )
    with connect(db_path) as db:
        insert(
            db,
            Source(
                id=1,
                url="https://example.test/ready",
                title="Fixture",
                niche=Niche.GAMES,
                source_allowed=True,
                input_key="url:https://example.test/ready",
                allowed_basis="campaign allowed-source list",
            ),
        )

    class Boom:
        def download(self, url: str, destination: Path) -> None:
            raise AssertionError(url)

    source_id, reused = ingest_video(
        settings,
        "https://example.test/ready",
        Niche.GAMES,
        allowed=True,
        basis="campaign allowed-source list",
        downloader=Boom(),
        free_bytes=lambda path: 0,
    )
    assert (source_id, reused) == (1, True)


def test_log_redacts_api_keys_and_skips_an_unwritable_directory(tmp_path: Path) -> None:
    secret = "deepgram-secret-value"
    settings = Settings(data_dir=tmp_path, deepgram_api_key=SecretStr(secret))
    configure_logging(settings)
    logging.getLogger("montaj").info("calling with %s", secret)
    text = (tmp_path / "logs" / "montaj.log").read_text(encoding="utf-8")
    assert secret not in text
    assert "[redacted]" in text
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("x", encoding="utf-8")
    configure_logging(Settings(data_dir=blocked))


def test_runbook_pins_the_yt_dlp_release() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    runbook = Path("docs/server.md").read_text(encoding="utf-8")
    assert YT_DLP_PIN in readme
    assert YT_DLP_PIN in runbook
    assert f"yt-dlp=={YT_DLP_PIN}" in runbook
    assert f"yt-dlp-ejs=={YT_DLP_EJS_PIN}" in runbook
    assert "8765" in runbook
