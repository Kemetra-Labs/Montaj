"""Import a permitted video, extract audio, and register reusable artifacts."""

import json
import math
import shutil
import sqlite3
import wave
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated, Protocol
from urllib.parse import urlsplit

import typer
from pydantic import BaseModel, Field, ValidationError

from montaj.config import Settings
from montaj.db import connect, fetch, insert, save_source
from montaj.housekeeping import HousekeepingError, disk_free, require_space
from montaj.media import MediaError, file_hash, run_tool, source_lock
from montaj.models import Campaign, CampaignRules, Niche, Source, utc_now


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


def _download_message(detail: str) -> str:
    """Classify yt-dlp errors using fixed messages and discard the raw output."""
    folded = detail.casefold()
    categories = (
        (
            ("private video", "video is private"),
            "The video is private. Request a permitted copy.",
        ),
        (
            ("age-restricted", "confirm your age", "age restriction"),
            "The video is age-restricted. Request a permitted copy.",
        ),
        (
            (
                "not available in your country",
                "not available from your location",
                "geo restricted",
            ),
            "The video is blocked in this region. Request a permitted copy.",
        ),
        (
            (
                "sign in",
                "login required",
                "http error 401",
                "http error 403",
                "drm protected",
                "drm-protected",
            ),
            "The source is restricted or access was denied. Check access to the permitted source.",
        ),
        (
            ("no supported javascript runtime",),
            "YouTube requires a supported JavaScript runtime. Install Deno or rebuild the Montaj image, then run montaj doctor.",
        ),
        (
            ("challenge solver script", "yt-dlp-ejs"),
            "The YouTube EJS solver could not be loaded. Install the matching yt-dlp-ejs package or rebuild the Montaj image, then run montaj doctor.",
        ),
        (
            (
                "requested format is not available",
                "no video formats found",
                "no formats found",
            ),
            "No usable video and audio format was available. Check the source format list or obtain a permitted media file with both streams.",
        ),
        (
            (
                "fragment",
                "contenttooshort",
                "did not get any data blocks",
                "downloaded file is empty",
                "incomplete read",
            ),
            "The download was incomplete. Retry the same permitted source.",
        ),
        (
            (
                "network",
                "timed out",
                "temporary failure",
                "connection reset",
                "connection refused",
                "getaddrinfo",
                "name resolution",
                "unable to resolve",
                "http error 429",
                "http error 5",
            ),
            "The download failed because of a network or server error. Check connectivity and retry.",
        ),
        (
            (
                "video unavailable",
                "video is unavailable",
                "has been removed",
                "http error 404",
            ),
            "The video was removed or is unavailable. Verify that the original link still works.",
        ),
        (
            (
                "ffmpeg not found",
                "ffmpeg is not installed",
                "install yt-dlp",
                "could not start yt-dlp",
            ),
            "Check that yt-dlp and FFmpeg are installed and runnable with montaj doctor.",
        ),
        (
            ("unsupported codec", "unsupported streams", "error merging formats"),
            "The downloaded streams could not be merged. Obtain a permitted file with supported video and audio streams.",
        ),
    )
    for phrases, message in categories:
        if any(phrase in folded for phrase in phrases):
            return message
    return "The download failed. Run montaj doctor and check the source's availability."


def download_failure(detail: str) -> SourceError:
    return SourceError(_download_message(detail), code=4)


class YtDlpDownloader:
    def download(self, url: str, destination: Path) -> None:
        target = destination.resolve()
        try:
            output = run_tool(
                "yt-dlp",
                [
                    "--ignore-config",
                    "--no-plugin-dirs",
                    "--no-remote-components",
                    "--xff",
                    "never",
                    "--no-playlist",
                    "--socket-timeout",
                    "20",
                    "--abort-on-unavailable-fragments",
                    "--format",
                    "bv*+ba/b",
                    "--encoding",
                    "utf-8",
                    "--no-simulate",
                    "--print",
                    "after_move:filepath",
                    "--output",
                    f"{str(target).replace('%', '%%')}.%(ext)s",
                    "--",
                    url,
                ],
                error_message=_download_message,
            )
        except MediaError as exc:
            raise SourceError(str(exc), code=4) from exc
        paths = output.splitlines()
        if len(paths) == 1:
            downloaded = Path(paths[0])
            if (
                downloaded.parent == target.parent
                and downloaded.stem == target.name
                and not downloaded.is_symlink()
                and downloaded.is_file()
                and downloaded.stat().st_size > 0
            ):
                downloaded.replace(target)
                return
        raise SourceError(
            "The download was incomplete. Retry the same permitted source.", code=4
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
        "-xerror",
        "-y",
        "-protocol_whitelist",
        "file,pipe",
        "-i",
        str(local_path),
        "-map",
        "0:V:0",
        "-map",
        "0:a:0",
    ]
    try:
        run_tool(
            "ffmpeg",
            [*args, "-c", "copy", "-movflags", "+faststart", str(video)],
            error_message=_media_message,
        )
    except MediaError:
        run_tool(
            "ffmpeg",
            [
                *args,
                "-c:v",
                "libx264",
                "-c:a",
                "aac",
                "-movflags",
                "+faststart",
                str(video),
            ],
            error_message=_media_message,
        )


_UNREADABLE_MEDIA = "The media could not be read as a video. It may be corrupt or incomplete. Retry the source or obtain a complete permitted copy."


def _media_message(detail: str) -> str:
    folded = detail.casefold()
    if "timed out" in folded:
        return "Media validation or normalization timed out. Retry with a complete permitted copy."
    if any(
        phrase in folded
        for phrase in (
            "unsupported codec",
            "decoder not found",
            "no decoder",
            "not currently supported",
        )
    ) or ("decoder" in folded and "not found" in folded):
        return "The media uses unsupported streams. Obtain a permitted copy with supported video and audio codecs."
    return _UNREADABLE_MEDIA


def _probe_input(path: Path) -> tuple[float, str | None]:
    if not path.is_file() or path.stat().st_size == 0:
        raise SourceError(_UNREADABLE_MEDIA, code=5)
    result = run_tool(
        "ffprobe",
        [
            "-v",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            "-show_entries",
            "stream=codec_type:stream_disposition=attached_pic:format=duration:format_tags=title",
            "-of",
            "json",
            str(path),
        ],
        error_message=_media_message,
    )
    try:
        probe = json.loads(result)
        streams = {
            stream.get("codec_type")
            for stream in probe.get("streams", [])
            if not stream.get("disposition", {}).get("attached_pic")
        }
        video_format = probe.get("format", {})
        title = video_format.get("tags", {}).get("title")
    except (AttributeError, TypeError, ValueError) as exc:
        raise SourceError(_UNREADABLE_MEDIA, code=5) from exc
    if "video" not in streams:
        raise SourceError(
            "The media has no usable video stream. Obtain a permitted video file.",
            code=5,
        )
    if "audio" not in streams:
        raise SourceError(
            "The media has no audio stream. Obtain a permitted copy with audio.", code=5
        )
    try:
        duration = float(video_format.get("duration"))
    except (TypeError, ValueError) as exc:
        raise SourceError(_UNREADABLE_MEDIA, code=5) from exc
    if duration <= 0 or not math.isfinite(duration):
        raise SourceError(_UNREADABLE_MEDIA, code=5)
    return duration, title if isinstance(title, str) else None


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
            duration, title = _probe_input(path)
            name = path.name if url is None else "Downloaded media"
            enforce_limits(name, duration, path.stat().st_size, settings)
            _prepare_video(path, video)
            _extract_audio(video, audio)
        except MediaError as exc:
            raise SourceError(str(exc), code=5) from exc
        title = title or (
            path.stem if url is None else urlsplit(url).hostname or "Untitled video"
        )
        enforce_limits(name, duration, video.stat().st_size, settings)
        metadata = SourceMetadata(
            title=title,
            duration=duration,
            url=url,
            video_hash=file_hash(video),
            audio_hash=file_hash(audio),
        )
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
            "-xerror",
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
        error_message=_media_message,
    )
    try:
        with wave.open(str(audio), "rb") as output:
            if not output.readframes(1):
                raise SourceError(
                    "The audio stream has no readable samples. Obtain a permitted copy with audio.",
                    code=5,
                )
    except (EOFError, OSError, wave.Error) as exc:
        raise SourceError(_UNREADABLE_MEDIA, code=5) from exc


def _permission(basis: str | None) -> str:
    text = (basis or "").strip()
    if not text:
        raise SourceError("Record why this source may be used with --basis.", code=2)
    if len(text) > 200:
        raise SourceError(
            "Keep --basis to a short description under 200 characters.", code=2
        )
    return text


def require_campaign_source(location: str, rules: CampaignRules) -> None:
    if not rules.allowed_sources:
        return
    folded = location.casefold()
    if any(item.casefold() in folded for item in rules.allowed_sources):
        return
    raise SourceError(
        "This source is not on the campaign's allowed-source list.", code=2
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
    basis: str | None = None,
    campaign_id: int | None = None,
    downloader: Downloader | None = None,
    free_bytes: Callable[[Path], int] | None = None,
    wait: float = 0,
) -> tuple[int, bool]:
    if not allowed:
        raise SourceError(
            "Only permitted sources can be processed. Confirm permission with --allowed."
        )
    permission = _permission(basis)
    if not settings.db_path.is_file():
        raise SourceError("Database is missing. Run montaj db init first.")
    key, url, path = _identity(location)
    with connect(settings.db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        campaign = fetch(db, Campaign, campaign_id) if campaign_id is not None else None
        if campaign_id is not None and campaign is None:
            raise SourceError(f"Campaign {campaign_id} does not exist.")
        if campaign is not None:
            require_campaign_source(location, campaign.rules)
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
                    allowed_basis=permission,
                    allowed_at=utc_now(),
                ),
            )
        else:
            source = Source.model_validate(dict(row))
    if source.id is None:
        raise SourceError("Could not assign a source ID.")
    directory = source_dir(settings, source.id)
    with source_lock(directory, command="ingest", wait=wait):
        with connect(settings.db_path) as db:
            db.execute("BEGIN IMMEDIATE")
            current = fetch(db, Source, source.id)
            if current is None or not current.source_allowed:
                raise SourceError(f"Source {source.id} lacks permission.")
            if current.allowed_basis not in {None, permission}:
                raise SourceError(
                    f"Source {source.id} already records a different permission basis."
                )
            if current.niche != niche or (
                campaign_id is not None and current.campaign_id != campaign_id
            ):
                raise SourceError(
                    f"Source {source.id} is already registered with different source settings."
                )
            current.allowed_basis = permission
            current.allowed_at = current.allowed_at or utc_now()
            if _ready(directory) is not None:
                if current.speech_status == "failed":
                    current.speech_status = None
                    current.failure_note = None
                save_source(db, current)
                return source.id, True
            directory.mkdir(parents=True, exist_ok=True)
            save_source(db, current)
        try:
            try:
                require_space(settings, path, free_bytes or disk_free)
            except HousekeepingError as exc:
                raise SourceError(str(exc), code=3) from exc
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
    basis: Annotated[
        str | None,
        typer.Option(help="Short reason this source may be used."),
    ] = None,
    wait: Annotated[
        float,
        typer.Option("--wait", min=0, help="Seconds to wait if this source is locked."),
    ] = 0,
) -> None:
    """Import a local video or a permitted video URL."""
    try:
        source_id, reused = ingest_video(
            Settings.from_env(),
            location,
            niche,
            allowed=allowed,
            basis=basis,
            campaign_id=campaign,
            wait=wait,
        )
    except SourceError as exc:
        typer.echo(f"Ingest failed: {exc}", err=True)
        raise typer.Exit(exc.code) from exc
    except (MediaError, OSError, ValueError, sqlite3.Error) as exc:
        typer.echo(f"Ingest failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"Source {source_id} {'reused' if reused else 'imported'}.")
