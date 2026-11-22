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
from montaj.db import connect, fetch, insert, save_source
from montaj.media import MediaError, file_hash, run_tool
from montaj.models import Campaign, Niche, Source


class SourceError(ValueError):
    """A source is unregistered, incomplete, or lacks permission."""

    def __init__(self, message: str, *, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


class SourceMetadata(BaseModel):
    title: str
    duration: float = Field(gt=0, allow_inf_nan=False)
    url: str | None = None
    video_hash: str
    audio_hash: str


class Downloader(Protocol):
    def download(self, url: str, destination: Path) -> None: ...


def enforce_limits(
    name: str, duration: float, size_bytes: int, settings: Settings
) -> None:
    """Refuse a source that is longer or larger than the configured ceilings."""
    minutes = duration / 60
    megabytes = size_bytes / (1024 * 1024)
    if minutes > settings.max_source_minutes + 1e-8:
        raise SourceError(
            f"{name} is {minutes:.1f} minutes; the limit is {settings.max_source_minutes:.0f}.",
            code=3,
        )
    if megabytes > settings.max_source_mb + 1e-8:
        raise SourceError(
            f"{name} is {megabytes:.1f} MB; the limit is {settings.max_source_mb:.0f}.",
            code=3,
        )


def download_failure(detail: str) -> SourceError:
    """Turn a yt-dlp failure into one plain message. No cookies or logins are added."""
    folded = detail.casefold()
    if "private" in folded:
        reason = "The video is private."
    elif "age" in folded:
        reason = "The video is age-restricted."
    elif "geo" in folded or "not available in your country" in folded:
        reason = "The video is blocked in this region."
    elif any(word in folded for word in ("network", "timed out", "temporary failure")):
        reason = "The download failed because the network was unavailable."
    elif any(word in folded for word in ("removed", "not found", "404", "unavailable")):
        reason = "The video was removed or is unavailable."
    else:
        reason = "The download failed."
    return SourceError(reason, code=4)


class YtDlpDownloader:
    def download(self, url: str, destination: Path) -> None:
        try:
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
        except MediaError as exc:
            raise download_failure(str(exc)) from exc


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
    settings: Settings,
    directory: Path,
    path: Path | None,
    url: str | None,
    downloader: Downloader,
) -> SourceMetadata:
    with TemporaryDirectory(prefix=".ingest-", dir=directory) as staging:
        root = Path(staging)
        if path is None:
            if url is None:
                raise SourceError("Enter a video URL or local file.")
            path = root / "download"
            try:
                downloader.download(url, path)
            except MediaError as exc:
                raise download_failure(str(exc)) from exc
        video = root / "video.mp4"
        audio = root / "audio.wav"
        try:
            _prepare_video(path, video)
            _extract_audio(video, audio)
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
        except MediaError as exc:
            raise SourceError(
                f"{path.name} could not be read as a video.", code=5
            ) from exc
        if not any(
            stream.get("codec_type") == "video" for stream in probe.get("streams", [])
        ):
            raise SourceError(f"{path.name} could not be read as a video.", code=5)
        video_format = probe.get("format", {})
        title = video_format.get("tags", {}).get("title") or (
            path.stem if url is None else urlsplit(url).hostname or "Untitled video"
        )
        try:
            metadata = SourceMetadata(
                title=title,
                duration=video_format.get("duration"),
                url=url,
                video_hash=file_hash(video),
                audio_hash=file_hash(audio),
            )
        except ValidationError as exc:
            raise SourceError(
                f"{path.name} could not be read as a video.", code=5
            ) from exc
        enforce_limits(path.name, metadata.duration, video.stat().st_size, settings)
        meta = root / "meta.json"
        meta.write_text(metadata.model_dump_json(indent=2) + "\n", encoding="utf-8")
        for artifact in (video, audio, meta):
            artifact.replace(directory / artifact.name)
        return metadata


def _extract_audio(video: Path, audio: Path) -> None:
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


def _mark_failed(settings: Settings, source_id: int, message: str) -> None:
    with connect(settings.db_path) as db:
        current = fetch(db, Source, source_id)
        if current is None:
            return
        current.speech_status = "failed"
        current.failure_note = message
        save_source(db, current)


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
        campaign = fetch(db, Campaign, campaign_id) if campaign_id is not None else None
        if campaign_id is not None and campaign is None:
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
            if current.speech_status == "failed":
                current.speech_status = None
                current.failure_note = None
            save_source(db, current)
            return source.id, True
        directory.mkdir(parents=True, exist_ok=True)
        save_source(db, current)
    try:
        metadata = _artifacts(
            settings, directory, path, url, downloader or YtDlpDownloader()
        )
    except SourceError as exc:
        _mark_failed(settings, source.id, str(exc))
        raise
    with connect(settings.db_path) as db:
        current = fetch(db, Source, source.id)
        if current is None:
            raise SourceError(f"Source {source.id} disappeared during ingest.")
        current.title = metadata.title
        current.speech_status = None
        current.failure_note = None
        save_source(db, current)
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
            Settings.from_env(),
            location,
            niche,
            allowed=allowed,
            campaign_id=campaign,
        )
    except SourceError as exc:
        typer.echo(f"Ingest failed: {exc}", err=True)
        raise typer.Exit(exc.code) from exc
    except (MediaError, OSError, ValueError, sqlite3.Error) as exc:
        typer.echo(f"Ingest failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"Source {source_id} {'reused' if reused else 'imported'}.")
