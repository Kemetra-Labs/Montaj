"""Smart reframing through real FFmpeg and PySceneDetect on synthetic footage.

The "face" is a white square, found by a tiny detector, so these tests exercise scene
cuts, crop planning, the filter graph and audio sync without any model weights.
"""

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from montaj.camera import CameraConfig, ReframePlan, ShotPlan, crop_fraction
from montaj.faces import FaceBox, VisionUnavailable
from montaj.reframe import (
    MAX_EXPRESSION_CHARS,
    AnalysisError,
    FrameSize,
    build_graph,
    fit_enable,
    x_expression,
)
from montaj.render import FFmpegRenderer, SmartReframer

np = pytest.importorskip("numpy")
pytest.importorskip("scenedetect")


class SquareDetector:
    """Reports the bright square as a face, like YuNet would report a head."""

    def __init__(self) -> None:
        self.calls = 0

    def detect(self, frame: Any) -> list[FaceBox]:
        self.calls += 1
        mask = frame.mean(axis=2) > 200
        if not mask.any():
            return []
        ys, xs = np.nonzero(mask)
        height, width = mask.shape
        return [
            FaceBox(
                xs.min() / width,
                ys.min() / height,
                (xs.max() - xs.min() + 1) / width,
                (ys.max() - ys.min() + 1) / height,
                0.95,
            )
        ]


class BrokenDetector:
    def detect(self, frame: Any) -> list[FaceBox]:
        raise VisionUnavailable("model failed to load")


def _two_shots(ffmpeg: str, path: Path, size: str = "640x360") -> Path:
    """Three seconds with the square on the right, a hard cut, then on the left."""
    width, height = (int(v) for v in size.split("x"))
    box = height // 4
    right, left = width - box - width // 16, width // 16
    y = (height - box) // 2
    subprocess.run(
        [
            ffmpeg, "-nostdin", "-v", "error", "-y",
            "-f", "lavfi", "-i", f"color=c=0x203040:s={size}:r=30:d=3,"
            f"drawbox=x={right}:y={y}:w={box}:h={box}:color=white:t=fill",
            "-f", "lavfi", "-i", f"color=c=0x705020:s={size}:r=30:d=3,"
            f"drawbox=x={left}:y={y}:w={box}:h={box}:color=white:t=fill",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=6",
            "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]",
            "-map", "[v]", "-map", "2:a", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", str(path),
        ],
        check=True,
    )  # fmt: skip
    return path


def _probe(path: Path) -> dict[str, Any]:
    out = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries",
            "stream=codec_type,width,height,r_frame_rate,duration", "-of", "json", str(path),
        ],
        check=True, capture_output=True, text=True,
    ).stdout  # fmt: skip
    return {s["codec_type"]: s for s in json.loads(out)["streams"]}


def _white_share(ffmpeg: str, path: Path, at: float) -> float:
    raw = subprocess.run(
        [
            ffmpeg, "-nostdin", "-v", "error", "-ss", str(at), "-i", str(path),
            "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-",
        ],
        check=True, capture_output=True,
    ).stdout  # fmt: skip
    pixels = np.frombuffer(raw, np.uint8)
    return float((pixels > 200).mean())


def _smart(detector: Any, fallback: str = "center") -> SmartReframer:
    return SmartReframer(
        lambda: detector,
        lambda crop: CameraConfig(crop, fallback),  # type: ignore[arg-type]
    )


@pytest.mark.media
def test_scene_cut_moves_the_crop_with_the_subject_and_keeps_sync(
    tmp_path: Path, ffmpeg: str
) -> None:
    source = _two_shots(ffmpeg, tmp_path / "source.mp4")
    smart, legacy = tmp_path / "smart.mp4", tmp_path / "legacy.mp4"
    detector = SquareDetector()
    report = FFmpegRenderer(reframer=_smart(detector)).render(source, smart, 0.0, 6.0)
    FFmpegRenderer().render(source, legacy, 0.0, 6.0)

    assert report.engine == "smart" and report.reason == "ok"
    assert [shot["layout"] for shot in report.shots] == ["track", "track"]
    assert report.shots[0]["end"] == pytest.approx(3.0, abs=0.1)
    assert detector.calls > 10
    streams = _probe(smart)
    assert (streams["video"]["width"], streams["video"]["height"]) == (1080, 1920)
    assert streams["video"]["r_frame_rate"] == "30/1"
    video_s, audio_s = (
        float(streams["video"]["duration"]),
        float(streams["audio"]["duration"]),
    )
    assert video_s == pytest.approx(6.0, abs=0.05)
    assert abs(video_s - audio_s) < 0.05
    for at in (1.5, 4.5):
        assert _white_share(ffmpeg, smart, at) > 0.05
        # The old centered crop loses the subject in both shots.
        assert _white_share(ffmpeg, legacy, at) < 0.001


@pytest.mark.media
def test_analysis_failure_falls_back_to_the_centered_render(
    tmp_path: Path, ffmpeg: str
) -> None:
    source = _two_shots(ffmpeg, tmp_path / "source.mp4")
    output = tmp_path / "out.mp4"
    report = FFmpegRenderer(reframer=_smart(BrokenDetector())).render(
        source, output, 0.5, 2.5
    )
    assert report.engine == "center"
    assert report.reason.startswith("analysis_failed")
    assert _probe(output)["video"]["width"] == 1080


@pytest.mark.media
def test_screen_content_without_faces_keeps_the_whole_frame(
    tmp_path: Path, ffmpeg: str
) -> None:
    source = _two_shots(ffmpeg, tmp_path / "source.mp4")
    output = tmp_path / "fit.mp4"

    class NoFaces:
        def detect(self, frame: Any) -> list[FaceBox]:
            return []

    report = FFmpegRenderer(reframer=_smart(NoFaces(), "fit")).render(
        source, output, 0.0, 6.0
    )
    assert {shot["layout"] for shot in report.shots} == {"fit"}
    assert _probe(output)["video"]["height"] == 1920
    # The full-width foreground keeps the square that a center crop would drop.
    assert _white_share(ffmpeg, output, 1.5) > 0.003


@pytest.mark.media
def test_portrait_sources_keep_the_previous_render_path(
    tmp_path: Path, ffmpeg: str
) -> None:
    source = _two_shots(ffmpeg, tmp_path / "tall.mp4", size="360x640")
    report = FFmpegRenderer(reframer=_smart(SquareDetector())).render(
        source, tmp_path / "out.mp4", 0.0, 2.0
    )
    assert (report.engine, report.reason) == ("center", "source_is_not_landscape")


def test_filter_description_for_center_track_and_fit_shots() -> None:
    size = FrameSize(1920, 1080)
    config = CameraConfig(crop_fraction(1920, 1080))
    center = ReframePlan((ShotPlan(0, 5, "center", "center_contains_faces"),))
    assert x_expression(center, 3414, config) is None
    assert (
        build_graph(center, size, config).filter
        == "fps=30,scale=3414:1920,crop=1080:1920,setsar=1"
    )
    track = ReframePlan(
        (
            ShotPlan(0, 2, "center", "x"),
            ShotPlan(2, 5, "track", "x", ((2, 0.5), (3, 0.6), (5, 0.6))),
            ShotPlan(5, 6, "fit", "x"),
        )
    )
    expression = x_expression(track, 3414, config)
    assert expression is not None and expression.startswith("clip(if(lt(t,2),")
    assert (
        "341.4*clip((t-3)" not in expression and "341.4*clip((t-2)/1,0,1)" in expression
    )
    assert fit_enable(track) == "gte(t,5)*lt(t,6)"
    graph = build_graph(track, size, config)
    assert graph.complex and graph.filter.endswith("[v]")


def test_overlong_crop_paths_are_rejected_before_rendering() -> None:
    path = tuple((i * 0.01, 0.1 + 0.4 * (i % 2)) for i in range(2000))
    plan = ReframePlan((ShotPlan(0, 20, "track", "x", path),))
    config = CameraConfig(crop_fraction(1920, 1080))
    assert len(x_expression(plan, 3414, config) or "") > MAX_EXPRESSION_CHARS
    with pytest.raises(AnalysisError):
        build_graph(plan, FrameSize(1920, 1080), config)
