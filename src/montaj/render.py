"""The single FFmpeg render used for every new clip.

One pass does the scene-aware crop, the safe loudness gain and the fades. Smart
reframing is optional: any missing piece, any unreliable shot and any analysis error
falls back to the centered crop that Montaj always used, and the report says why.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from montaj.camera import CameraConfig, ReframePlan, crop_fraction
from montaj.faces import FaceDetector, VisionUnavailable
from montaj.loudness import (
    VERIFY_TRUE_PEAK_DBTP,
    Loudness,
    LoudnessPolicy,
    backoff_gain_db,
    measure_loudness,
    policy_gain_db,
)
from montaj.media import MediaError, run_tool
from montaj.reframe import (
    Analysis,
    AnalysisError,
    FFmpegFrames,
    FrameSize,
    FrameSource,
    SceneCutFinder,
    analyze,
    build_graph,
    center_only,
    eligible,
    graph_args,
    legacy_filter,
    plan_for,
    probe_size,
    shot_report,
)

REPORT_VERSION = 1


@dataclass(slots=True)
class RenderReport:
    """What a render did, saved next to the clip for review and for the owner."""

    engine: str = "center"
    reason: str = ""
    shots: list[dict[str, Any]] = field(default_factory=list)
    loudness: dict[str, float | str | None] = field(default_factory=dict)
    analysis_seconds: float = 0.0

    def as_json(self) -> dict[str, Any]:
        return {
            "version": REPORT_VERSION,
            "engine": self.engine,
            "reason": self.reason,
            "shots": self.shots,
            "loudness": self.loudness,
            "analysis_seconds": round(self.analysis_seconds, 2),
        }


class Reframer(Protocol):
    def plan(
        self, video: Path, start: float, duration: float, size: FrameSize
    ) -> tuple[ReframePlan, CameraConfig, Analysis]: ...


class SmartReframer:
    """Scenes, then faces, then a safe crop path for one clip."""

    def __init__(
        self,
        detector: Callable[[], FaceDetector],
        config_for: Callable[[float], CameraConfig],
        *,
        frames: Callable[[FrameSize], FrameSource] = FFmpegFrames,
        cutter: Callable[[], Any] = SceneCutFinder,
    ) -> None:
        self._make_detector = detector
        self._detector: FaceDetector | None = None
        self._config_for = config_for
        self._frames = frames
        self._cutter = cutter

    def plan(
        self, video: Path, start: float, duration: float, size: FrameSize
    ) -> tuple[ReframePlan, CameraConfig, Analysis]:
        if self._detector is None:
            self._detector = self._make_detector()
        config = self._config_for(crop_fraction(size.width, size.height))
        found = analyze(
            video,
            start,
            duration,
            frames=self._frames(size),
            detector=self._detector,
            cutter=self._cutter(),
        )
        return plan_for(found, duration, config), config, found


def _audio_filter(duration: float, gain: float) -> str:
    fade = min(0.15, duration / 2)
    fades = f"afade=t=in:st=0:d={fade:.6f},afade=t=out:st={duration - fade:.6f}:d={fade:.6f}"
    return f"volume={gain:.2f}dB,{fades}" if gain else fades


class FFmpegRenderer:
    def __init__(
        self,
        policy: LoudnessPolicy | None = None,
        reframer: Reframer | None = None,
    ) -> None:
        self.policy = policy
        self.reframer = reframer

    def _video(
        self, video: Path, start: float, duration: float, report: RenderReport
    ) -> list[str]:
        graph = legacy_filter()
        args = ["-vf", graph, "-map", "0:v:0"]
        if self.reframer is None:
            report.reason = "legacy_engine"
            return args
        began = time.monotonic()
        try:
            size = probe_size(video)
            if not eligible(size):
                report.reason = "source_is_not_landscape"
                return args
            plan, config, _ = self.reframer.plan(video, start, duration, size)
            built = build_graph(plan, size, config)
        except (AnalysisError, VisionUnavailable, MediaError, OSError) as exc:
            report.reason = f"analysis_failed: {exc}"
            plan = center_only(duration, CameraConfig(0.0), "analysis_failed")
            report.shots = shot_report(plan)
            return args
        finally:
            report.analysis_seconds = time.monotonic() - began
        report.engine = "smart"
        report.reason = "ok"
        report.shots = shot_report(plan)
        return graph_args(built)

    def _gain(
        self, video: Path, start: float, duration: float, report: RenderReport
    ) -> float:
        if self.policy is None:
            return 0.0
        heard = measure_loudness(video, start=start, duration=duration)
        gain = policy_gain_db(heard, self.policy)
        report.loudness = {
            "policy": self.policy.tag(),
            "before_lufs": heard.integrated,
            "before_dbtp": heard.true_peak,
            "gain_db": gain,
        }
        return gain

    def _encode(
        self,
        video: Path,
        output: Path,
        start: float,
        duration: float,
        video_args: list[str],
        gain: float,
    ) -> None:
        run_tool(
            "ffmpeg",
            [
                "-nostdin", "-v", "error", "-y", "-protocol_whitelist", "file,pipe",
                "-ss", str(start), "-i", str(video), "-t", str(duration),
                *video_args, "-map", "0:a:0", "-af", _audio_filter(duration, gain),
                "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p",
                "-threads:v", "2", "-c:a", "aac", "-movflags", "+faststart", str(output),
            ],
        )  # fmt: skip

    def _verify(self, output: Path, report: RenderReport) -> Loudness | None:
        if self.policy is None:
            return None
        after = measure_loudness(output)
        report.loudness["after_lufs"] = after.integrated
        report.loudness["after_dbtp"] = after.true_peak
        return after

    def render(
        self, video: Path, output: Path, start: float, end: float
    ) -> RenderReport:
        duration = end - start
        report = RenderReport()
        video_args = self._video(video, start, duration, report)
        gain = self._gain(video, start, duration, report)
        self._encode(video, output, start, duration, video_args, gain)
        after = self._verify(output, report)
        assert self.policy is None or after is not None
        if (
            after is not None
            and gain
            and self.policy is not None
            and after.true_peak > VERIFY_TRUE_PEAK_DBTP
        ):
            reduced = backoff_gain_db(gain, after.true_peak, self.policy.ceiling_dbtp)
            self._encode(video, output, start, duration, video_args, reduced)
            report.loudness["gain_db"] = reduced
            report.loudness["backed_off_from_db"] = gain
            self._verify(output, report)
        return report
