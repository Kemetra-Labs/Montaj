"""Analyze a clip for smart reframing and describe the crop as an FFmpeg filter.

One low-resolution decode through the system FFmpeg feeds both PySceneDetect and
the face detector, so the analysis never depends on which codecs a Python wheel
can decode. The final render still happens in a single FFmpeg pass.
"""

import json
import shutil
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from montaj.camera import (
    CameraConfig,
    ReframePlan,
    Sample,
    ShotPlan,
    center_left,
    crop_fraction,
    plan_clip,
)
from montaj.faces import FaceDetector, VisionUnavailable
from montaj.media import MediaError, run_tool

ANALYSIS_FPS = 15
ANALYSIS_WIDTH = 320
FACE_EVERY = 4
MIN_SCENE_FRAMES = 4
OUT_WIDTH, OUT_HEIGHT = 1080, 1920
MAX_EXPRESSION_CHARS = 12000


class AnalysisError(RuntimeError):
    """The clip could not be analyzed. The centered crop is used instead."""


class FrameSource(Protocol):
    def frames(self, video: Path, start: float, duration: float) -> Iterator[Any]: ...


class CutFinder(Protocol):
    def feed(self, index: int, frame: Any) -> list[int]: ...


@dataclass(frozen=True, slots=True)
class FrameSize:
    width: int
    height: int


def probe_size(video: Path) -> FrameSize:
    payload = run_tool(
        "ffprobe",
        [
            "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height", "-of", "json", str(video),
        ],
    )  # fmt: skip
    try:
        stream = json.loads(payload)["streams"][0]
        return FrameSize(int(stream["width"]), int(stream["height"]))
    except (KeyError, IndexError, ValueError) as exc:
        raise MediaError("ffprobe returned no video size.") from exc


class FFmpegFrames:
    """Decode a clip once at low resolution and low frame rate."""

    def __init__(self, size: FrameSize, *, threads: int = 2) -> None:
        self.size = size
        self.threads = threads
        self.height = max(2, round(ANALYSIS_WIDTH * size.height / size.width / 2) * 2)

    def frames(self, video: Path, start: float, duration: float) -> Iterator[Any]:
        import numpy

        executable = shutil.which("ffmpeg")
        if executable is None:
            raise AnalysisError("Install ffmpeg and add it to PATH.")
        command = [
            executable, "-nostdin", "-v", "error", "-threads", str(self.threads),
            "-protocol_whitelist", "file,pipe", "-ss", str(start), "-i", str(video),
            "-t", str(duration), "-map", "0:v:0", "-an", "-sn",
            "-vf", f"fps={ANALYSIS_FPS},scale={ANALYSIS_WIDTH}:{self.height}",
            "-f", "rawvideo", "-pix_fmt", "bgr24", "-",
        ]  # fmt: skip
        frame_bytes = ANALYSIS_WIDTH * self.height * 3
        with subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        ) as process:
            assert process.stdout is not None
            while chunk := process.stdout.read(frame_bytes):
                if len(chunk) < frame_bytes:
                    break
                yield numpy.frombuffer(chunk, numpy.uint8).reshape(
                    self.height, ANALYSIS_WIDTH, 3
                )
            stderr = process.stderr.read() if process.stderr else b""
            if process.wait() != 0:
                detail = stderr.decode("utf-8", errors="replace").strip()[-300:]
                raise AnalysisError(f"ffmpeg could not decode the clip: {detail}")


class SceneCutFinder:
    """PySceneDetect's AdaptiveDetector fed frame by frame."""

    def __init__(self) -> None:
        try:
            from scenedetect.detectors import AdaptiveDetector
        except ImportError as exc:
            raise VisionUnavailable(
                "Install the vision extra (uv sync --extra vision)."
            ) from exc
        self._detector = AdaptiveDetector(min_scene_len=MIN_SCENE_FRAMES)

    def feed(self, index: int, frame: Any) -> list[int]:
        return [int(cut) for cut in self._detector.process_frame(index, frame)]


@dataclass(frozen=True, slots=True)
class Analysis:
    samples: tuple[Sample, ...]
    cuts: tuple[float, ...]
    frames: int


def analyze(
    video: Path,
    start: float,
    duration: float,
    *,
    frames: FrameSource,
    detector: FaceDetector,
    cutter: CutFinder,
) -> Analysis:
    """Find scene cuts and sample faces. Times are seconds from the clip start."""
    cut_frames: list[int] = []
    samples: list[Sample] = []
    count = 0
    for index, frame in enumerate(frames.frames(video, start, duration)):
        cut_frames.extend(cutter.feed(index, frame))
        if index % FACE_EVERY == 0:
            samples.append(Sample(index / ANALYSIS_FPS, tuple(detector.detect(frame))))
        count += 1
    if count == 0:
        raise AnalysisError("No frames could be decoded for analysis.")
    # A cut is the first frame of the new scene. Switch half an output frame earlier.
    cuts = tuple(
        sorted({max(0.0, index / ANALYSIS_FPS - 1 / 60) for index in cut_frames})
    )
    near = 1.5 / ANALYSIS_FPS
    clean = tuple(s for s in samples if all(abs(s.t - c) >= near for c in cuts))
    return Analysis(clean, cuts, count)


def plan_for(analysis: Analysis, duration: float, config: CameraConfig) -> ReframePlan:
    return plan_clip(analysis.samples, analysis.cuts, duration, config)


def center_only(duration: float, config: CameraConfig, reason: str) -> ReframePlan:
    return ReframePlan((ShotPlan(0.0, duration, config.fallback, reason),))


# --- FFmpeg filter description -------------------------------------------------


def _number(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".") or "0"


def _shot_expression(shot: ShotPlan, scaled_width: int, centered: float) -> str:
    if shot.layout != "track":
        return _number(centered * scaled_width)
    points = [(t, x * scaled_width) for t, x in shot.path]
    base = points[0][1]
    terms = [
        f"{_number(x1 - x0)}*clip((t-{_number(t0)})/{_number(t1 - t0)},0,1)"
        for (t0, x0), (t1, x1) in zip(points, points[1:], strict=False)
        if abs(x1 - x0) > 0.01 and t1 > t0
    ]
    return "+".join([_number(base), *terms])


def x_expression(
    plan: ReframePlan, scaled_width: int, config: CameraConfig
) -> str | None:
    """Crop-left in scaled pixels as a function of clip time, or None if centered."""
    shots = plan.shots
    if all(shot.layout != "track" for shot in shots):
        return None
    centered = center_left(config)
    expression = _shot_expression(shots[-1], scaled_width, centered)
    for shot in reversed(shots[:-1]):
        inner = _shot_expression(shot, scaled_width, centered)
        expression = f"if(lt(t,{_number(shot.end)}),{inner},{expression})"
    maximum = scaled_width - OUT_WIDTH
    return f"clip({expression},0,{maximum})"


def fit_enable(plan: ReframePlan) -> str | None:
    spans = [
        f"gte(t,{_number(shot.start)})*lt(t,{_number(shot.end)})"
        for shot in plan.shots
        if shot.layout == "fit"
    ]
    return "+".join(spans) if spans else None


def legacy_filter() -> str:
    return (
        f"scale={OUT_WIDTH}:{OUT_HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={OUT_WIDTH}:{OUT_HEIGHT},setsar=1,fps=30"
    )


@dataclass(frozen=True, slots=True)
class VideoGraph:
    """A -vf chain, or a -filter_complex graph whose output pad is [v]."""

    filter: str
    complex: bool


def build_graph(plan: ReframePlan, size: FrameSize, config: CameraConfig) -> VideoGraph:
    scaled_width = round(OUT_HEIGHT * size.width / size.height / 2) * 2
    expression = x_expression(plan, scaled_width, config)
    if expression is not None and len(expression) > MAX_EXPRESSION_CHARS:
        raise AnalysisError("The crop path is too complex to render in one pass.")
    crop = f"crop={OUT_WIDTH}:{OUT_HEIGHT}"
    if expression is not None:
        crop += f":x='{expression}':y=0"
    main = f"scale={scaled_width}:{OUT_HEIGHT},{crop},setsar=1"
    enable = fit_enable(plan)
    if enable is None:
        return VideoGraph(f"fps=30,{main}", complex=False)
    blur_w, blur_h = OUT_WIDTH // 4, OUT_HEIGHT // 4
    graph = (
        f"[0:v:0]fps=30,split=3[a][b][c];"
        f"[a]{main}[main];"
        f"[b]scale={blur_w}:{blur_h}:force_original_aspect_ratio=increase,"
        f"crop={blur_w}:{blur_h},boxblur=8:2,scale={OUT_WIDTH}:{OUT_HEIGHT},setsar=1[bg];"
        f"[c]scale={OUT_WIDTH}:-2[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2[fit];"
        f"[main][fit]overlay=enable='{enable}'[v]"
    )
    return VideoGraph(graph, complex=True)


def graph_args(graph: VideoGraph) -> list[str]:
    if graph.complex:
        return ["-filter_complex", graph.filter, "-map", "[v]"]
    return ["-vf", graph.filter, "-map", "0:v:0"]


def eligible(size: FrameSize) -> bool:
    """Only landscape and square sources need a crop. Taller ones keep the old path."""
    return crop_fraction(size.width, size.height) < 0.98


def shot_report(plan: ReframePlan) -> list[dict[str, Any]]:
    return [
        {
            "start": round(shot.start, 3),
            "end": round(shot.end, 3),
            "layout": shot.layout,
            "reason": shot.reason,
            "keyframes": [[round(t, 3), round(x, 4)] for t, x in shot.path],
        }
        for shot in plan.shots
    ]
