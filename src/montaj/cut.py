"""Render selected moments as centered vertical video."""

import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated, Protocol

import typer

from montaj.config import Settings
from montaj.db import connect, fetch, save_clip
from montaj.ingest import load_source
from montaj.media import MediaError, file_hash, run_tool, source_lock
from montaj.models import Clip, ClipStage, ClipStatus
from montaj.pipeline import ClipInputs, is_stale, stage_input_hash
from montaj.rank import load_rank

CUT_STYLE = "center-1080x1920-h264-aac-30fps-fade015-v1"


class Renderer(Protocol):
    def render(self, video: Path, output: Path, start: float, end: float) -> None: ...


class FFmpegRenderer:
    def render(self, video: Path, output: Path, start: float, end: float) -> None:
        duration = end - start
        fade = min(0.15, duration / 2)
        audio_filter = f"afade=t=in:st=0:d={fade:.6f},afade=t=out:st={duration - fade:.6f}:d={fade:.6f}"
        run_tool(
            "ffmpeg",
            [
                "-nostdin",
                "-v",
                "error",
                "-y",
                "-protocol_whitelist",
                "file,pipe",
                "-ss",
                str(start),
                "-i",
                str(video),
                "-t",
                str(duration),
                "-map",
                "0:v:0",
                "-map",
                "0:a:0",
                "-vf",
                "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1,fps=30",
                "-af",
                audio_filter,
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                "-crf",
                "20",
                "-pix_fmt",
                "yuv420p",
                "-threads:v",
                "2",
                "-c:a",
                "aac",
                "-movflags",
                "+faststart",
                str(output),
            ],
        )


def render_inputs(
    video: Path, transcript_hash: str = "", options: dict[ClipStage, str] | None = None
) -> ClipInputs:
    return ClipInputs(
        source_hash=file_hash(video),
        transcript_hash=transcript_hash,
        stage_options={ClipStage.CUT: CUT_STYLE, **(options or {})},
    )


def selected_clips(
    settings: Settings,
    source_id: int,
    directory: Path,
    clip_id: int | None = None,
) -> list[Clip]:
    ids = load_rank(directory).selected_ids
    if clip_id is not None:
        if clip_id not in ids:
            raise ValueError(
                f"Clip {clip_id} is outside the ranked selection for source {source_id}."
            )
        ids = [clip_id]
    result = []
    with connect(settings.db_path) as db:
        for selected_id in ids:
            clip = fetch(db, Clip, selected_id)
            if clip is None or clip.source_id != source_id:
                raise ValueError(f"Ranked clip {selected_id} is missing. Rerun rank.")
            if clip.status not in (ClipStatus.REJECTED, ClipStatus.POSTED):
                result.append(clip)
            elif clip_id is not None:
                raise ValueError(f"Clip {clip_id} is {clip.status}.")
    return result


def render_clip(
    settings: Settings,
    video: Path,
    clip: Clip,
    inputs: ClipInputs,
    renderer: Renderer,
) -> bool:
    """Render one clip and return whether it needed new work."""
    if clip.id is None:
        raise ValueError("The clip must exist in the database before rendering.")
    relative = Path("clips") / str(clip.id) / "raw.mp4"
    output = settings.data_dir / relative
    if (
        clip.raw_path == relative.as_posix()
        and output.is_file()
        and not is_stale(clip, ClipStage.CUT, inputs)
    ):
        return False
    duration = clip.end_s - clip.start_s
    if duration <= 0:
        raise ValueError(f"Clip {clip.id} must have a positive duration.")
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=output.parent) as temporary:
        staged = Path(temporary) / "raw.mp4"
        try:
            renderer.render(video, staged, clip.start_s, clip.end_s)
        except MediaError as exc:
            raise MediaError(f"Clip {clip.id}: {exc}") from exc
        staged.replace(output)
    key = stage_input_hash(clip, ClipStage.CUT, inputs)
    changed = clip.input_hashes.get(ClipStage.CUT) != key
    clip.input_hashes[ClipStage.CUT] = key
    clip.raw_path = relative.as_posix()
    if changed:
        if clip.status in (ClipStatus.APPROVED, ClipStatus.EXPORTED):
            clip.status = ClipStatus.DRAFTED
            clip.changed_since_review = True
        else:
            clip.status = ClipStatus.RENDERED
    with connect(settings.db_path) as db:
        save_clip(db, clip)
    return True


def cut_source(
    settings: Settings,
    source_id: int,
    clip_id: int | None = None,
    *,
    renderer: Renderer | None = None,
) -> tuple[int, int]:
    _, metadata, directory = load_source(settings, source_id)
    video = directory / "video.mp4"
    changed = 0
    with source_lock(directory):
        clips = selected_clips(settings, source_id, directory, clip_id)
        inputs = render_inputs(video)
        cutter = renderer or FFmpegRenderer()
        for clip in clips:
            if clip.end_s > metadata.duration + 0.05:
                raise ValueError(f"Clip {clip.id} ends after the source video.")
            changed += render_clip(settings, video, clip, inputs, cutter)
    return changed, len(clips) - changed


def cut(
    source_id: Annotated[int, typer.Argument(min=1)],
    clip: Annotated[int | None, typer.Option(min=1)] = None,
) -> None:
    """Render ranked clips as vertical MP4 files."""
    try:
        rendered, reused = cut_source(Settings.from_env(), source_id, clip)
    except (MediaError, OSError, ValueError, sqlite3.Error) as exc:
        typer.echo(f"Cut failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"Source {source_id}: {rendered} rendered, {reused} reused.")
