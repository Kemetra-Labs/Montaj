"""Render selected moments as vertical video with the unified engine."""

import dataclasses
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated, Protocol

import typer

from montaj.config import Settings
from montaj.db import connect, fetch, save_clip
from montaj.engine import (
    CUT_STYLE,
    camera_config,
    cut_recipe,
    loudness_policy,
    recipe_lookup,
    record_recipe,
    vision_status,
)
from montaj.faces import YuNetDetector
from montaj.ingest import load_source
from montaj.media import MediaError, atomic_text, file_hash, source_lock
from montaj.models import Clip, ClipStage, ClipStatus, Niche
from montaj.pipeline import (
    ClipInputs,
    LegacyRecipe,
    effective_options,
    is_stale,
    stage_input_hash,
)
from montaj.rank import has_review_decision, load_rank, require_current_rank
from montaj.render import FFmpegRenderer, RenderReport, SmartReframer

__all__ = ["CUT_STYLE", "FFmpegRenderer", "Renderer", "cut_style", "render_inputs"]


class Renderer(Protocol):
    def render(
        self, video: Path, output: Path, start: float, end: float
    ) -> RenderReport | None: ...


def cut_style(settings: Settings, niche: Niche | None = None) -> str:
    """Name the render recipe so any engine or policy change re-renders the clip."""
    return cut_recipe(settings, niche)


def build_renderer(settings: Settings, niche: Niche | None = None) -> FFmpegRenderer:
    """Smart reframing when it can run here, the centered crop when it cannot."""
    policy = loudness_policy(settings)
    if (
        settings.engine == "legacy"
        or not vision_status(settings.resolved_yunet_path).ready
    ):
        return FFmpegRenderer(policy)
    model = settings.resolved_yunet_path
    reframer = SmartReframer(
        lambda: YuNetDetector(model), lambda crop: camera_config(crop, niche)
    )
    return FFmpegRenderer(policy, reframer)


def render_inputs(
    video: Path,
    transcript_hash: str = "",
    options: dict[ClipStage, str] | None = None,
    *,
    style: str = CUT_STYLE,
    legacy: tuple[LegacyRecipe, ...] = (LegacyRecipe(CUT_STYLE),),
    recorded: Callable[[int], LegacyRecipe | None] | None = None,
) -> ClipInputs:
    return ClipInputs(
        source_hash=file_hash(video),
        transcript_hash=transcript_hash,
        stage_options={ClipStage.CUT: style, **(options or {})},
        legacy=legacy,
        recorded=recorded,
    )


def selected_clips(
    settings: Settings,
    source_id: int,
    directory: Path,
    clip_id: int | None = None,
) -> list[Clip]:
    rank = load_rank(directory)
    ids = rank.selected_ids
    result = []
    with connect(settings.db_path) as db:
        uses_brief = require_current_rank(db, source_id, directory, rank)
        if clip_id is not None:
            if clip_id not in ids:
                raise ValueError(
                    f"Clip {clip_id} is outside the ranked selection for source {source_id}."
                )
            ids = [clip_id]
        for selected_id in ids:
            clip = fetch(db, Clip, selected_id)
            if clip is None or clip.source_id != source_id:
                raise ValueError(f"Ranked clip {selected_id} is missing. Rerun rank.")
            if uses_brief and clip_id is None and has_review_decision(clip):
                continue
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
    used, _ = effective_options(clip, inputs)
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=output.parent) as temporary:
        staged = Path(temporary) / "raw.mp4"
        try:
            report = renderer.render(video, staged, clip.start_s, clip.end_s)
        except MediaError as exc:
            raise MediaError(f"Clip {clip.id}: {exc}") from exc
        staged.replace(output)
    if report is not None:
        atomic_text(
            output.with_name("render.json"),
            json.dumps(report.as_json(), indent=2) + "\n",
        )
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
    if used is not None:
        record_recipe(settings.data_dir, clip.id, used)
    return True


def cut_source(
    settings: Settings,
    source_id: int,
    clip_id: int | None = None,
    *,
    renderer: Renderer | None = None,
    wait: float = 0,
    upgrade: bool = False,
) -> tuple[int, int]:
    source, metadata, directory = load_source(settings, source_id)
    video = directory / "video.mp4"
    changed = 0
    with source_lock(directory, command="cut", wait=wait):
        clips = selected_clips(settings, source_id, directory, clip_id)
        inputs = render_inputs(
            video,
            style=cut_style(settings, source.niche),
            recorded=recipe_lookup(settings.data_dir),
        )
        if upgrade:
            inputs = dataclasses.replace(inputs, legacy=(), recorded=None)
        cutter = renderer or build_renderer(settings, source.niche)
        for clip in clips:
            if clip.end_s > metadata.duration + 0.05:
                raise ValueError(f"Clip {clip.id} ends after the source video.")
            changed += render_clip(settings, video, clip, inputs, cutter)
    return changed, len(clips) - changed


def cut(
    source_id: Annotated[int, typer.Argument(min=1)],
    clip: Annotated[int | None, typer.Option(min=1)] = None,
    wait: Annotated[
        float,
        typer.Option("--wait", min=0, help="Seconds to wait if this source is locked."),
    ] = 0,
    upgrade: Annotated[
        bool,
        typer.Option(
            "--upgrade",
            help="Re-render reviewed clips with the current engine. They return to review.",
        ),
    ] = False,
) -> None:
    """Render ranked clips as vertical MP4 files."""
    try:
        rendered, reused = cut_source(
            Settings.from_env(), source_id, clip, wait=wait, upgrade=upgrade
        )
    except (MediaError, OSError, ValueError, sqlite3.Error) as exc:
        typer.echo(f"Cut failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"Source {source_id}: {rendered} rendered, {reused} reused.")
