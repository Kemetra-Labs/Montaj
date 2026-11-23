"""Build word-synced ASS captions and burn them into vertical renders."""

import shutil
import sqlite3
import tomllib
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated

import typer
from pydantic import BaseModel, Field

from montaj.caption_layout import TimedWord, caption_events
from montaj.config import Settings
from montaj.cut import CUT_STYLE, cut_style, render_inputs, selected_clips
from montaj.db import connect, save_clip
from montaj.engine import recipe_lookup, record_recipe, source_niche
from montaj.ingest import load_source
from montaj.media import MediaError, atomic_text, file_hash, run_tool, source_lock
from montaj.models import Clip, ClipStage, ClipStatus
from montaj.pipeline import (
    ClipInputs,
    LegacyRecipe,
    effective_options,
    input_hash,
    is_stale,
    stage_input_hash,
)
from montaj.rank import clip_words
from montaj.transcribe import Transcript, Word

_STYLE_HEADER = (
    "Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
    "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
    "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding"
)


class CaptionStyle(BaseModel):
    font_family: str
    size: int = Field(ge=30, le=110)
    outline: float = Field(ge=0, le=20)
    highlight: str = Field(pattern=r"^[0-9a-fA-F]{6}$")
    margin_bottom: int = Field(ge=384, le=1450)
    words_per_line: int = Field(ge=3, le=5)
    max_gap: float = Field(default=0.5, ge=0, le=1.5)


# The style captions used before the unified engine. Reviewed clips stay pinned to it.
LEGACY_CAPTION_STYLE = {
    "font_family": "Noto Sans Arabic",
    "size": 66,
    "outline": 4.0,
    "highlight": "FFDC52",
    "margin_bottom": 450,
    "words_per_line": 4,
}
CAPTION_ENGINE = "ass-word-highlight-v2"


def load_caption_style() -> CaptionStyle:
    path = Path(__file__).with_name("caption_style.toml")
    with path.open("rb") as style:
        return CaptionStyle.model_validate(tomllib.load(style))


def source_inputs(
    settings: Settings,
    directory: Path,
    *,
    rules_hash: str = "",
    extra: dict[ClipStage, str] | None = None,
) -> ClipInputs:
    """Build the clip-stage fingerprints shared by captions, draft, and check."""
    style = load_caption_style()
    base = caption_inputs(
        directory / "video.mp4",
        directory / "transcript.json",
        settings.resolved_font_path,
        style,
        cut_style(settings, source_niche(settings, directory)),
        settings.data_dir,
    )
    return ClipInputs(
        source_hash=base.source_hash,
        transcript_hash=base.transcript_hash,
        rules_hash=rules_hash,
        stage_options={**base.stage_options, **(extra or {})},
        legacy=base.legacy,
        recorded=base.recorded,
    )


def caption_inputs(
    video: Path,
    transcript_path: Path,
    font: Path,
    style: CaptionStyle,
    cut: str = CUT_STYLE,
    data_dir: Path | None = None,
) -> ClipInputs:
    if not font.is_file():
        raise ValueError(
            "Arabic font is missing. Run montaj doctor or set MONTAJ_FONT_PATH."
        )
    return render_inputs(
        video,
        file_hash(transcript_path),
        {
            ClipStage.CAPTIONS: input_hash(
                style.model_dump(), file_hash(font), CAPTION_ENGINE
            ),
        },
        style=cut,
        legacy=(
            LegacyRecipe(
                CUT_STYLE,
                input_hash(
                    LEGACY_CAPTION_STYLE, file_hash(font), "ass-word-highlight-v1"
                ),
            ),
        ),
        recorded=recipe_lookup(data_dir) if data_dir is not None else None,
    )


def _ass_time(centiseconds: int) -> str:
    second, fraction = divmod(centiseconds, 100)
    minute, second = divmod(second, 60)
    hour, minute = divmod(minute, 60)
    return f"{hour}:{minute:02}:{second:02}.{fraction:02}"


def _safe_word(text: str) -> str:
    # Preserve the visible text without allowing transcript content to create ASS commands.
    return " ".join(text.split()).translate(str.maketrans("\\{}", "＼｛｝"))


def _event_text(lines: tuple[tuple[str, ...], ...], active: int, color: str) -> str:
    rows, index = [], 0
    for line in lines:
        parts = []
        for text in line:
            word = _safe_word(text)
            if index == active:
                word = rf"{{\c&H{color}&}}{word}{{\c&HFFFFFF&}}"
            parts.append(word)
            index += 1
        rows.append(" ".join(parts))
    return r"\N".join(rows)


def render_ass(words: list[Word], clip: Clip, style: CaptionStyle) -> str:
    color = style.highlight[4:6] + style.highlight[2:4] + style.highlight[0:2]
    header = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\nWrapStyle: 2\n\n"
        f"[V4+ Styles]\nFormat: {_STYLE_HEADER}\n"
        f"Style: Default,{style.font_family},{style.size},&H00FFFFFF,&H00FFFFFF,&H00121212,&H00000000,"
        f"0,0,0,0,100,100,0,0,1,{style.outline},0,2,100,100,{style.margin_bottom},1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    duration = clip.end_s - clip.start_s
    timed = [
        TimedWord(
            max(0.0, word.start - clip.start_s),
            min(word.end, clip.end_s) - clip.start_s,
            " ".join(word.text.split()),
        )
        for word in sorted(words, key=lambda word: (word.start, word.end))
    ]
    timed = [word for word in timed if word.end > word.start and word.text]
    events = []
    for event in caption_events(
        timed,
        duration,
        size=style.size,
        outline=style.outline,
        words_per_line=style.words_per_line,
        max_gap=style.max_gap,
    ):
        first, last = round(event.start * 100), round(event.end * 100)
        if last <= first:
            continue
        line = _event_text(event.lines, event.active, color)
        events.append(
            f"Dialogue: 0,{_ass_time(first)},{_ass_time(last)},Default,,0,0,0,,{line}"
        )
    return header + "\n".join(events) + "\n"


def burn_clip(
    settings: Settings,
    clip: Clip,
    inputs: ClipInputs,
    words: list[Word],
    style: CaptionStyle,
    font: Path,
) -> bool:
    if clip.id is None or not clip.raw_path:
        raise ValueError("Render the raw clip before adding captions.")
    raw = settings.data_dir / clip.raw_path
    if not raw.is_file() or is_stale(clip, ClipStage.CUT, inputs):
        raise ValueError(f"Clip {clip.id} has no current raw render. Run montaj cut.")
    relative = Path("clips") / str(clip.id) / "final.mp4"
    final = settings.data_dir / relative
    ass_path = final.with_name("captions.ass")
    if (
        clip.final_path == relative.as_posix()
        and final.is_file()
        and ass_path.is_file()
        and not is_stale(clip, ClipStage.CAPTIONS, inputs)
    ):
        return False
    if not words:
        raise ValueError(f"Clip {clip.id} has no timed words for captions.")
    cut_used, used = effective_options(clip, inputs)
    ass = render_ass(words, clip, style)
    with TemporaryDirectory(dir=final.parent) as temp:
        staging = Path(temp)
        fonts = staging / "fonts"
        fonts.mkdir()
        shutil.copyfile(font, fonts / font.name)
        (staging / "captions.ass").write_text(ass, encoding="utf-8")
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
                str(raw.resolve()),
                "-vf",
                "ass=captions.ass:fontsdir=fonts",
                "-map",
                "0:v:0",
                "-map",
                "0:a:0",
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
                "copy",
                "-movflags",
                "+faststart",
                "final.mp4",
            ],
            cwd=staging,
        )
        (staging / "final.mp4").replace(final)
        atomic_text(ass_path, ass)
    key = stage_input_hash(clip, ClipStage.CAPTIONS, inputs)
    changed = clip.input_hashes.get(ClipStage.CAPTIONS) != key
    clip.input_hashes[ClipStage.CAPTIONS] = key
    clip.final_path = relative.as_posix()
    if changed:
        if clip.status in (ClipStatus.APPROVED, ClipStatus.EXPORTED):
            clip.status = ClipStatus.DRAFTED
            clip.changed_since_review = True
        else:
            clip.status = ClipStatus.CAPTIONED
    with connect(settings.db_path) as db:
        save_clip(db, clip)
    if cut_used is not None:
        record_recipe(settings.data_dir, clip.id, cut_used, used)
    return True


def caption_source(
    settings: Settings,
    source_id: int,
    clip_id: int | None = None,
    *,
    wait: float = 0,
) -> tuple[int, int]:
    _, _, directory = load_source(settings, source_id)
    transcript_path = directory / "transcript.json"
    style = load_caption_style()
    font = settings.resolved_font_path
    changed = 0
    with source_lock(directory, command="captions", wait=wait):
        clips = selected_clips(settings, source_id, directory, clip_id)
        transcript = Transcript.model_validate_json(transcript_path.read_bytes())
        source, _, _ = load_source(settings, source_id)
        inputs = caption_inputs(
            directory / "video.mp4",
            transcript_path,
            font,
            style,
            cut_style(settings, source.niche),
            settings.data_dir,
        )
        for clip in clips:
            words = clip_words(transcript, clip.start_s, clip.end_s)
            changed += burn_clip(settings, clip, inputs, words, style, font)
    return changed, len(clips) - changed


def captions(
    source_id: Annotated[int, typer.Argument(min=1)],
    clip: Annotated[int | None, typer.Option(min=1)] = None,
    wait: Annotated[
        float,
        typer.Option("--wait", min=0, help="Seconds to wait if this source is locked."),
    ] = 0,
) -> None:
    """Burn word-synced captions into ranked clips."""
    try:
        rendered, reused = caption_source(
            Settings.from_env(), source_id, clip, wait=wait
        )
    except (MediaError, OSError, ValueError, sqlite3.Error) as exc:
        typer.echo(f"Captions failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"Source {source_id}: {rendered} captioned, {reused} reused.")
