"""Offline one-clip audio and caption A/B for MJ-3.

Reads the saved raw render, cached transcript and database read-only. Makes no API calls, writes no
database rows and never touches the saved clip files. Every output goes to --out-dir.
"""

import argparse
import json
import re
import shutil
import sqlite3
import subprocess
from collections.abc import Sequence
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from montaj.captions import CaptionStyle, load_caption_style, render_ass
from montaj.config import Settings
from montaj.models import Clip
from montaj.rank import clip_words
from montaj.transcribe import Transcript, Word
from scripts.pilot_paths import workspace_path

TRUE_PEAK_CEILING = -2.0
LOUDNESS_RANGE = 11.0
SIDE_MARGIN = 100  # MarginL/MarginR in the ASS style header, in 1080-wide units.
VIDEO_WIDTH = 1080
VIDEO_HEIGHT = 1920
SCAN_TOP = 960  # Captions sit in the lower half; scan from here down.
SCAN_FPS = 10
_JSON_BLOCK = re.compile(r"\{[^{}]*\}")
_FONT_LINE = re.compile(r"fontselect:.*")


def parse_loudnorm(stderr: str) -> dict[str, Any]:
    """Return the last JSON block FFmpeg printed, with numeric strings converted."""
    blocks = _JSON_BLOCK.findall(stderr)
    if not blocks:
        raise ValueError("FFmpeg printed no loudnorm JSON.")
    parsed: dict[str, Any] = json.loads(blocks[-1])
    return {key: _number(value) for key, value in parsed.items()}


def _number(value: str) -> float | str:
    try:
        return float(value)
    except ValueError:
        return value


def max_linear_target(integrated: float, true_peak: float, ceiling: float) -> float:
    """Highest integrated target reachable by constant gain without passing the ceiling."""
    return integrated + (ceiling - true_peak)


def frame_bounds(frame: bytes, width: int) -> tuple[int, int, int, int] | None:
    """Bounds (x1, x2, y1, y2) of non-zero pixels in a gray frame, or None if blank."""
    rows = [frame[start : start + width] for start in range(0, len(frame), width)]
    lit = [index for index, row in enumerate(rows) if any(row)]
    if not lit:
        return None
    merged = 0
    for index in lit:
        merged |= int.from_bytes(rows[index], "big")
    columns = merged.to_bytes(width, "big")
    return (
        width - len(columns.lstrip(b"\0")),
        len(columns.rstrip(b"\0")) - 1,
        lit[0],
        lit[-1],
    )


def gap_stats(words: Sequence[Word], short: float = 0.15) -> dict[str, float]:
    """Blank time and flash risk implied by per-word highlight events."""
    ordered = sorted(words, key=lambda word: (word.start, word.end))
    gaps = [b.start - a.end for a, b in zip(ordered, ordered[1:], strict=False)]
    shown = [
        min(a.end, b.start) - a.start
        for a, b in zip(ordered, ordered[1:], strict=False)
    ]
    blank = [gap for gap in gaps if gap > 0]
    return {
        "words": len(ordered),
        "gaps": len(blank),
        "gaps_at_least_0_1s": sum(gap >= 0.1 for gap in blank),
        "blank_seconds": round(sum(blank), 3),
        "longest_gap": round(max(blank, default=0.0), 3),
        "events_under_0_15s": sum(duration < short for duration in shown),
        "shortest_event": round(min(shown, default=0.0), 3),
    }


def ffmpeg(
    args: list[str], *, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    done = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-nostats", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if done.returncode:
        raise RuntimeError(f"ffmpeg failed ({done.returncode}): {done.stderr[-600:]}")
    return done


def measure(path: Path) -> dict[str, Any]:
    """First loudnorm pass on the decoded first audio stream."""
    filter_ = (
        f"loudnorm=I=-20:TP={TRUE_PEAK_CEILING}:LRA={LOUDNESS_RANGE}:print_format=json"
    )
    run = ffmpeg(["-i", str(path), "-map", "0:a:0", "-af", filter_, "-f", "null", "-"])
    return parse_loudnorm(run.stderr)


def measured_only(stats: dict[str, Any]) -> dict[str, Any]:
    """Drop the filter's simulated output_* fields, which describe no real file."""
    return {key: value for key, value in stats.items() if key.startswith("input_")}


def astats(path: Path) -> dict[str, float]:
    run = ffmpeg(["-i", str(path), "-map", "0:a:0", "-af", "astats", "-f", "null", "-"])
    overall = run.stderr.rsplit("Overall", 1)[-1]
    found: dict[str, float] = {}
    for label in ("RMS level dB", "RMS trough dB", "Peak level dB", "Noise floor dB"):
        match = re.search(rf"{label}: (\S+)", overall)
        if match:
            found[label] = float(match.group(1))
    return found


def video_md5(path: Path) -> str:
    run = ffmpeg(["-i", str(path), "-map", "0:v:0", "-c", "copy", "-f", "md5", "-"])
    return run.stdout.strip().removeprefix("MD5=")


def stream_timing(path: Path) -> dict[str, dict[str, str]]:
    probe = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries",
            "stream=codec_type,start_time,duration,bit_rate", "-of", "json", str(path),
        ],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    return {item["codec_type"]: item for item in json.loads(probe.stdout)["streams"]}


def normalize(
    source: Path, output: Path, first: dict[str, Any], target: float, bitrate: str
) -> dict[str, Any]:
    """Second loudnorm pass in linear mode with the video stream copied."""
    filter_ = (
        f"loudnorm=I={target}:TP={TRUE_PEAK_CEILING}:LRA={LOUDNESS_RANGE}"
        f":measured_I={first['input_i']}:measured_TP={first['input_tp']}"
        f":measured_LRA={first['input_lra']}:measured_thresh={first['input_thresh']}"
        f":offset={first['target_offset']}:linear=true:print_format=json"
    )
    run = ffmpeg(
        [
            "-y", "-i", str(source), "-map", "0:v:0", "-map", "0:a:0", "-c:v", "copy",
            "-af", filter_, "-ar", "48000", "-c:a", "aac", "-b:a", bitrate,
            "-movflags", "+faststart", str(output),
        ]
    )  # fmt: skip
    return parse_loudnorm(run.stderr)


def audio_step(args: argparse.Namespace) -> dict[str, Any]:
    raw = args.data_dir / "clips" / str(args.clip_id) / "raw.mp4"
    args.out_dir.mkdir(parents=True, exist_ok=True)
    first = measure(raw)
    headroom = max_linear_target(first["input_i"], first["input_tp"], TRUE_PEAK_CEILING)
    bitrate = stream_timing(raw)["audio"].get("bit_rate", "128000")
    result: dict[str, Any] = {
        "source": str(raw),
        "before": measured_only(first),
        "before_astats": astats(raw),
        "max_linear_target_lufs": round(headroom, 2),
        "aac_bitrate": bitrate,
        "variants": [],
    }
    for target in args.targets:
        output = args.out_dir / f"raw-norm{target:g}.mp4"
        second = normalize(raw, output, first, target, bitrate)
        after = measure(output)
        result["variants"].append(
            {
                "target": target,
                "file": output.name,
                "pass2": second,
                "decoded_after": measured_only(after),
                "after_astats": astats(output),
                "gain_db": round(after["input_i"] - first["input_i"], 2),
                "peak_ok": after["input_tp"] <= TRUE_PEAK_CEILING,
                "video_md5_same": video_md5(output) == video_md5(raw),
                "timing": stream_timing(output),
            }
        )
    result["raw_timing"] = stream_timing(raw)
    return result


def clip_context(args: argparse.Namespace) -> tuple[Clip, list[Word], Path, Path]:
    uri = f"file:{(args.data_dir / 'montaj.db').as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as db:
        row = db.execute(
            "select start_s, end_s from clips where id = ?", (args.clip_id,)
        ).fetchone()
    if row is None:
        raise SystemExit(f"Clip {args.clip_id} is not in the database.")
    clip = Clip(id=args.clip_id, source_id=1, start_s=row[0], end_s=row[1])
    transcript = Transcript.model_validate_json(
        (args.data_dir / "sources/1/transcript.json").read_bytes()
    )
    words = clip_words(transcript, clip.start_s, clip.end_s)
    raw = args.data_dir / "clips" / str(args.clip_id) / "raw.mp4"
    return clip, words, raw, Settings(data_dir=args.data_dir).resolved_font_path


def variant(base: CaptionStyle, **changes: Any) -> CaptionStyle:
    return CaptionStyle.model_validate({**base.model_dump(), **changes})


def _stage(staging: Path, ass: str, font: Path) -> None:
    (staging / "fonts").mkdir(exist_ok=True)
    shutil.copyfile(font, staging / "fonts" / font.name)
    (staging / "captions.ass").write_text(ass, encoding="utf-8")


def painted_frames(
    ass: str, font: Path, seconds: float
) -> list[tuple[float, int, int, int, int]]:
    """Render captions over black and return (time, x1, x2, y1, y2) for frames with text."""
    height = VIDEO_HEIGHT - SCAN_TOP
    chain = (
        f"ass=captions.ass:fontsdir=fonts,crop={VIDEO_WIDTH}:{height}:0:{SCAN_TOP},"
        f"lutyuv=y='if(gt(val,20),255,0)',format=gray,fps={SCAN_FPS}"
    )
    source = f"color=c=black:s={VIDEO_WIDTH}x{VIDEO_HEIGHT}:r=30:d={seconds:.3f}"
    frames = []
    with TemporaryDirectory() as temp:
        staging = Path(temp)
        _stage(staging, ass, font)
        command = [
            "ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", source,
            "-vf", chain, "-pix_fmt", "gray", "-f", "rawvideo", "-",
        ]  # fmt: skip
        with subprocess.Popen(command, cwd=staging, stdout=subprocess.PIPE) as process:
            assert process.stdout is not None
            index = 0
            while chunk := process.stdout.read(VIDEO_WIDTH * height):
                found = frame_bounds(chunk, VIDEO_WIDTH)
                if found:
                    x1, x2, y1, y2 = found
                    frames.append(
                        (index / SCAN_FPS, x1, x2, y1 + SCAN_TOP, y2 + SCAN_TOP)
                    )
                index += 1
        if process.returncode:
            raise RuntimeError("ffmpeg could not render the caption bounds.")
    return frames


def extents(ass: str, font: Path, seconds: float) -> dict[str, Any]:
    """Painted text bounds, including the outline, sampled at SCAN_FPS."""
    frames = painted_frames(ass, font, seconds)
    widths = [x2 - x1 + 1 for _, x1, x2, _, _ in frames]
    widest = max(range(len(frames)), key=lambda index: widths[index])
    limit = VIDEO_WIDTH - SIDE_MARGIN
    return {
        "sampled_frames_with_text": len(frames),
        "max_width_px": widths[widest],
        "widest_at_s": frames[widest][0],
        "left_min_px": min(frame[1] for frame in frames),
        "right_max_px": max(frame[2] for frame in frames),
        "top_min_px": min(frame[3] for frame in frames),
        "bottom_max_px": max(frame[4] for frame in frames),
        "frames_outside_side_margins": sum(
            frame[1] < SIDE_MARGIN or frame[2] > limit for frame in frames
        ),
        "available_width_px": VIDEO_WIDTH - 2 * SIDE_MARGIN,
    }


def burn(video: Path, ass: str, font: Path, output: Path) -> str:
    """Burn captions using the same FFmpeg arguments as montaj.captions.burn_clip."""
    with TemporaryDirectory() as temp:
        staging = Path(temp)
        _stage(staging, ass, font)
        run = ffmpeg(
            [
                "-loglevel", "verbose", "-y", "-protocol_whitelist", "file,pipe",
                "-i", str(video.resolve()), "-vf", "ass=captions.ass:fontsdir=fonts",
                "-map", "0:v:0", "-map", "0:a:0", "-c:v", "libx264", "-preset", "fast",
                "-crf", "20", "-pix_fmt", "yuv420p", "-threads:v", "2", "-c:a", "copy",
                "-movflags", "+faststart", "final.mp4",
            ],
            cwd=staging,
        )  # fmt: skip
        shutil.move(staging / "final.mp4", output)
    return "\n".join(sorted(set(_FONT_LINE.findall(run.stderr))))


def sweep_step(args: argparse.Namespace) -> dict[str, Any]:
    clip, words, _, font = clip_context(args)
    base = load_caption_style()
    seconds = clip.end_s - clip.start_s
    rows = []
    for size in args.sizes:
        outline = round(base.outline * size / base.size, 1)
        style = variant(base, size=size, outline=outline)
        rows.append(
            {
                "size": size,
                "outline": outline,
                **extents(render_ass(words, clip, style), font, seconds),
            }
        )
    return {"gaps": gap_stats(words), "sweep": rows}


def sheet(videos: dict[str, Path], times: list[float], out_dir: Path) -> list[str]:
    """Side-by-side frames at phone size (360x640 per clip) for each time."""
    written = []
    names = list(videos)
    for stamp in times:
        output = out_dir / f"phone-{stamp:05.2f}s.png"
        inputs: list[str] = []
        for path in videos.values():
            inputs += ["-ss", f"{stamp}", "-i", str(path)]
        labels = "".join(f"[{i}:v]scale=360:640[v{i}];" for i in range(len(names)))
        stack = "".join(f"[v{i}]" for i in range(len(names)))
        graph = f"{labels}{stack}hstack=inputs={len(names)}[out]"
        ffmpeg(
            [
                "-y",
                *inputs,
                "-filter_complex",
                graph,
                "-map",
                "[out]",
                "-frames:v",
                "1",
                str(output),
            ]
        )
        written.append(output.name)
    return written


def captions_step(args: argparse.Namespace) -> dict[str, Any]:
    clip, words, raw, font = clip_context(args)
    base = load_caption_style()
    pilot = variant(base, size=args.pilot_size, outline=args.pilot_outline)
    ass_a, ass_b = render_ass(words, clip, base), render_ass(words, clip, pilot)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    stored = args.data_dir / "clips" / str(args.clip_id) / "captions.ass"
    outputs = {
        "A-current": args.out_dir / "captions-A-current.mp4",
        "B-pilot": args.out_dir / "captions-B-pilot.mp4",
    }
    fonts = burn(raw, ass_a, font, outputs["A-current"])
    burn(raw, ass_b, font, outputs["B-pilot"])
    if args.normalized:
        outputs["C-audio-only"] = args.out_dir / "audio-C-normalized.mp4"
        outputs["D-combined"] = args.out_dir / "combined-D-pilot.mp4"
        burn(args.normalized, ass_a, font, outputs["C-audio-only"])
        burn(args.normalized, ass_b, font, outputs["D-combined"])
    (args.out_dir / "captions-A.ass").write_text(ass_a, encoding="utf-8")
    (args.out_dir / "captions-B.ass").write_text(ass_b, encoding="utf-8")
    seconds = clip.end_s - clip.start_s
    extents_a, extents_b = extents(ass_a, font, seconds), extents(ass_b, font, seconds)
    times = [args.times[0], extents_a["widest_at_s"], *args.times[1:]]
    return {
        "style_a": base.model_dump(),
        "style_b": pilot.model_dump(),
        "ass_a_identical_to_saved": stored.read_bytes().replace(b"\r\n", b"\n")
        == ass_a.encode("utf-8"),
        "libass_font_selection": fonts,
        "extents_a": extents_a,
        "extents_b": extents_b,
        "gaps": gap_stats(words),
        "video_md5": {name: video_md5(path) for name, path in outputs.items()},
        "saved_final_md5": video_md5(
            args.data_dir / "clips" / str(args.clip_id) / "final.mp4"
        ),
        "sheets": sheet(
            {k: outputs[k] for k in ("A-current", "B-pilot")}, times, args.out_dir
        ),
    }


def _workspace(value: str) -> Path:
    return workspace_path(Path(value))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=_workspace, required=True)
    parser.add_argument("--out-dir", type=_workspace, required=True)
    parser.add_argument("--clip-id", type=int, default=36)
    commands = parser.add_subparsers(dest="command", required=True)
    audio = commands.add_parser("audio", help="Two-pass linear loudnorm sweep.")
    audio.add_argument("--targets", type=float, nargs="+", default=[-20, -21, -23])
    audio.set_defaults(run=audio_step)
    sweep = commands.add_parser("sweep", help="Measure caption width per font size.")
    sweep.add_argument("--sizes", type=int, nargs="+", default=[66, 72, 78, 84, 90])
    sweep.set_defaults(run=sweep_step)
    pair = commands.add_parser(
        "captions", help="Burn current and pilot caption styles."
    )
    pair.add_argument("--pilot-size", type=int, required=True)
    pair.add_argument("--pilot-outline", type=float, required=True)
    pair.add_argument(
        "--normalized", type=_workspace, help="Raw render with normalized audio."
    )
    pair.add_argument("--times", type=float, nargs="+", default=[5.0, 20.0])
    pair.set_defaults(run=captions_step)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    result = args.run(args)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    text = json.dumps(result, indent=2, ensure_ascii=False)
    (args.out_dir / f"{args.command}.json").write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
