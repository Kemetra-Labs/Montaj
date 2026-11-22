"""Import a permitted video, extract audio, and register reusable artifacts."""

import json
import shutil
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated, Protocol
from urllib.parse import urlsplit

import typer
from pydantic import BaseModel, Field, ValidationError

from montaj.config import Settings
from montaj.db import connect, fetch, insert
from montaj.media import MediaError, file_hash, run_tool
from montaj.models import Campaign, Niche, Source


class SourceError(ValueError):
    """A source is unregistered, incomplete, or lacks permission."""


class SourceMetadata(BaseModel):
    title: str
    duration: float = Field(gt=0, allow_inf_nan=False)
    url: str | None = None
    video_hash: str
    audio_hash: str


class Downloader(Protocol):
    def download(self, url: str, destination: Path) -> None: ...


class YtDlpDownloader:
    def download(self, url: str, destination: Path) -> None:
        run_tool(
            "yt-dlp",
            [
                "--ignore-config",
                "--no-playlist",
                "--socket-timeout",
                "20",
                "--no-part",
                "--format",
                "best[ext=mp4]/best",
                "--output",
                str(destination),
                "--",
                url,
            ],
        )


def source_dir(settings: Settings, source_id: int) -> Path:
    return settings.data_dir / "sources" / str(source_id)


def _ready(directory: Path) -> SourceMetadata | None:
    if not all(
        (directory / name).is_file() for name in ("video.mp4", "audio.wav", "meta.json")
    ):
        return None
    try:
        return SourceMetadata.model_validate_json(
            (directory / "meta.json").read_bytes()
        )
    except (OSError, ValidationError):
        return None


def load_source(
    settings: Settings, source_id: int
) -> tuple[Source, SourceMetadata, Path]:
    if not settings.db_path.is_file():
        raise SourceError("Database is missing. Run montaj db init first.")
    with connect(settings.db_path) as db:
        source = fetch(db, Source, source_id)
    if source is None or not source.source_allowed:
        raise SourceError(f"Source {source_id} is missing or lacks permission.")
    directory = source_dir(settings, source_id)
    meta = _ready(directory)
    if meta is None:
        raise SourceError(f"Source {source_id} is incomplete. Rerun ingest first.")
    return source, meta, directory


def _identity(location: str) -> tuple[str, str | None, Path | None]:
    if "://" in location:
        try:
            url = urlsplit(location)
        except ValueError as exc:
            raise SourceError("Enter a valid HTTPS or HTTP video URL.") from exc
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username is not None
        ):
            raise SourceError(
                "Enter an HTTPS or HTTP video URL without embedded credentials."
            )
        return "url:" + location, location, None
    path = Path(location).expanduser().resolve()
    if not path.is_file():
        raise SourceError("Video file does not exist.")
    return "sha256:" + file_hash(path), None, path


def _prepare_video(local_path: Path, video: Path) -> None:
    if local_path.suffix.lower() == ".mp4":
        shutil.copyfile(local_path, video)
        return
    args = [
        "-nostdin",
        "-v",
        "error",
        "-y",
        "-protocol_whitelist",
        "file,pipe",
        "-i",
        str(local_path),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0",
    ]
    try:
        run_tool("ffmpeg", [*args, "-c", "copy", "-movflags", "+faststart", str(video)])
    except MediaError:
        run_tool("ffmpeg", [*args, "-c:v", "libx264", "-c:a", "aac", str(video)])


def _artifacts(
    directory: Path, path: Path | None, url: str | None, downloader: Downloader
) -> SourceMetadata:
    with TemporaryDirectory(prefix=".ingest-", dir=directory) as staging:
        root = Path(staging)
        if path is None:
            if url is None:
                raise SourceError("Enter a video URL or local file.")
            path = root / "download"
            downloader.download(url, path)
        video = root / "video.mp4"
        audio = root / "audio.wav"
        _prepare_video(path, video)
        run_tool(
            "ffmpeg",
            [
                "-nostdin",
                "-v",
                "error",
                "-y",
                "-protocol_whitelist",
                "file,pipe",
                "-i",
                str(video),
                "-map",
                "0:a:0",
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                str(audio),
            ],
        )
        probe = json.loads(
            run_tool(
                "ffprobe",
                [
                    "-v",
                    "error",
                    "-protocol_whitelist",
                    "file,pipe",
                    "-show_entries",
                    "stream=codec_type:format=duration:format_tags=title",
                    "-of",
                    "json",
                    str(video),
                ],
            )
        )
        if not any(
            stream.get("codec_type") == "video" for stream in probe.get("streams", [])
        ):
            raise SourceError("The file has no video stream.")
        video_format = probe.get("format", {})
        title = video_format.get("tags", {}).get("title") or (
            path.stem if url is None else urlsplit(url).hostname or "Untitled video"
        )
        metadata = SourceMetadata(
            title=title,
            duration=video_format.get("duration"),
            url=url,
            video_hash=file_hash(video),
            audio_hash=file_hash(audio),
        )
        meta = root / "meta.json"
        meta.write_text(metadata.model_dump_json(indent=2) + "\n", encoding="utf-8")
        for artifact in (video, audio, meta):
            artifact.replace(directory / artifact.name)
        return metadata


def ingest_video(
    settings: Settings,
    location: str,
    niche: Niche,
    *,
    allowed: bool,
    campaign_id: int | None = None,
    downloader: Downloader | None = None,
) -> tuple[int, bool]:
    if not allowed:
        raise SourceError(
            "Only permitted sources can be processed. Confirm permission with --allowed."
        )
    if not settings.db_path.is_file():
        raise SourceError("Database is missing. Run montaj db init first.")
    key, url, path = _identity(location)
    with connect(settings.db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        if campaign_id is not None and fetch(db, Campaign, campaign_id) is None:
            raise SourceError(f"Campaign {campaign_id} does not exist.")
        row = db.execute("SELECT * FROM sources WHERE input_key = ?", (key,)).fetchone()
        if row is None:
            source = insert(
                db,
                Source(
                    url=url,
                    path=str(path) if path else None,
                    title=location,
                    niche=niche,
                    campaign_id=campaign_id,
                    source_allowed=True,
                    input_key=key,
                ),
            )
        else:
            source = Source.model_validate(dict(row))
    if source.id is None:
        raise SourceError("Could not assign a source ID.")
    directory = source_dir(settings, source.id)
    with connect(settings.db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        current = fetch(db, Source, source.id)
        if current is None or not current.source_allowed:
            raise SourceError(f"Source {source.id} lacks permission.")
        if current.niche != niche or (
            campaign_id is not None and current.campaign_id != campaign_id
        ):
            raise SourceError(
                f"Source {source.id} is already registered with different source settings."
            )
        if _ready(directory) is not None:
            return source.id, True
        directory.mkdir(parents=True, exist_ok=True)
        metadata = _artifacts(directory, path, url, downloader or YtDlpDownloader())
        db.execute(
            "UPDATE sources SET title = ? WHERE id = ?", (metadata.title, source.id)
        )
    return source.id, False


def ingest(
    location: str,
    niche: Annotated[Niche, typer.Option()],
    campaign: Annotated[int | None, typer.Option(min=1)] = None,
    allowed: Annotated[
        bool,
        typer.Option("--allowed", help="Confirm permission to process the source."),
    ] = False,
) -> None:
    """Import a local video or a permitted video URL."""
    try:
        source_id, reused = ingest_video(
            Settings.from_env(), location, niche, allowed=allowed, campaign_id=campaign
        )
    except (MediaError, OSError, ValueError, sqlite3.Error) as exc:
        typer.echo(f"Ingest failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"Source {source_id} {'reused' if reused else 'imported'}.")
