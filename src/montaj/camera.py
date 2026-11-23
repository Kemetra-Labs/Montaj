"""Choose a safe 9:16 crop path from scenes and face detections.

This module is pure: it knows nothing about video files, OpenCV or FFmpeg. It never
claims to know who is speaking. It only decides whether a crop can keep every
reliable face in frame, and otherwise reports why the shot falls back.

Coordinates are fractions of the source frame width. A crop is described by its left
edge, so a 16:9 source has a crop 0.316 wide that can sit anywhere in [0, 0.684].
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from montaj.faces import MIN_SCORE, FaceBox

Layout = Literal["center", "track", "fit"]

PATH_FPS = 15.0
MIN_SHOT_SECONDS = 0.25
MIN_COVERAGE = 0.6
RELATIVE_FACE_SIZE = 0.45
FACE_PAD = 0.25  # of the faces' union width, on each side
EDGE_MARGIN = 0.04  # of the crop width, on each side
MAX_SPEED = 0.2  # fractions of the source width per second
SMOOTH_POINTS = 5
SIMPLIFY_TOLERANCE = 0.0007
EPS = 1e-9


@dataclass(frozen=True, slots=True)
class Sample:
    """Faces seen at one moment, in seconds from the start of the clip."""

    t: float
    faces: tuple[FaceBox, ...]


@dataclass(frozen=True, slots=True)
class ShotPlan:
    start: float
    end: float
    layout: Layout
    reason: str
    path: tuple[tuple[float, float], ...] = ()


@dataclass(frozen=True, slots=True)
class CameraConfig:
    crop_width: float
    fallback: Layout = "center"
    min_face_height: float = 0.07


@dataclass(frozen=True, slots=True)
class ReframePlan:
    shots: tuple[ShotPlan, ...]

    def counts(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for shot in self.shots:
            result[shot.layout] = result.get(shot.layout, 0) + 1
        return result


def crop_fraction(width: int, height: int) -> float:
    """Width of a 9:16 crop as a fraction of the source width."""
    return min(1.0, (9 / 16) * height / width)


def center_left(config: CameraConfig) -> float:
    return (1 - config.crop_width) / 2


def _union(
    faces: Sequence[FaceBox], config: CameraConfig
) -> tuple[float, float] | None:
    usable = [
        face
        for face in faces
        if face.score >= MIN_SCORE and face.height >= config.min_face_height
    ]
    if not usable:
        return None
    tallest = max(face.height for face in usable)
    kept = [face for face in usable if face.height >= RELATIVE_FACE_SIZE * tallest]
    return min(face.left for face in kept), max(face.right for face in kept)


def _interval(span: tuple[float, float], config: CameraConfig) -> tuple[float, float]:
    """Crop-left positions that keep the padded faces inside the crop."""
    left, right = span
    pad = FACE_PAD * (right - left) + EDGE_MARGIN * config.crop_width
    low = max(0.0, right + pad - config.crop_width)
    high = min(1 - config.crop_width, left - pad)
    return low, high


def _interpolate(
    points: list[tuple[float, float, float]], t: float
) -> tuple[float, float]:
    if t <= points[0][0]:
        return points[0][1], points[0][2]
    for (t0, lo0, hi0), (t1, lo1, hi1) in zip(points, points[1:], strict=False):
        if t <= t1:
            ratio = (t - t0) / (t1 - t0) if t1 > t0 else 1.0
            return lo0 + (lo1 - lo0) * ratio, hi0 + (hi1 - hi0) * ratio
    return points[-1][1], points[-1][2]


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _grid(start: float, end: float, anchors: Sequence[float]) -> list[float]:
    """Regular path times plus every sample time, so constraints hold exactly."""
    steps = max(1, round((end - start) * PATH_FPS))
    regular = [start + (end - start) * index / steps for index in range(steps + 1)]
    merged = sorted({*regular, *(t for t in anchors if start < t < end)})
    return [t for i, t in enumerate(merged) if i == 0 or t - merged[i - 1] > 1e-6]


def _reachable(
    lows: list[float], highs: list[float], steps: list[float]
) -> list[tuple[float, float]] | None:
    """Backward pass: crop positions from which every later interval stays reachable."""
    reach = [(lows[-1], highs[-1])]
    for index in range(len(lows) - 2, -1, -1):
        low, high, step = lows[index], highs[index], steps[index]
        next_low, next_high = reach[-1]
        new_low = max(low, next_low - step)
        new_high = min(high, next_high + step)
        if new_low > new_high + EPS:
            return None
        reach.append((new_low, max(new_low, new_high)))
    reach.reverse()
    return reach


def _follow(
    reach: list[tuple[float, float]], steps: list[float], start_at: float
) -> list[float]:
    """Forward pass: stay still until forced, then move no faster than allowed."""
    path = [_clamp(start_at, *reach[0])]
    for (low, high), step in zip(reach[1:], steps, strict=True):
        previous = path[-1]
        path.append(
            _clamp(previous, max(low, previous - step), min(high, previous + step))
        )
    return path


def _smooth(path: list[float], lows: list[float], highs: list[float]) -> list[float]:
    half = SMOOTH_POINTS // 2
    current = path
    for _ in range(3):
        blurred = []
        for index in range(len(current)):
            window = current[max(0, index - half) : index + half + 1]
            blurred.append(sum(window) / len(window))
        current = [
            _clamp(value, low, high)
            for value, low, high in zip(blurred, lows, highs, strict=True)
        ]
    return current


def _limit(
    path: list[float], lows: list[float], highs: list[float], steps: list[float]
) -> list[float] | None:
    """Project a smoothed path back inside the face intervals and the speed limit."""
    result = [_clamp(path[0], lows[0], highs[0])]
    for value, low, high, step in zip(
        path[1:], lows[1:], highs[1:], steps, strict=True
    ):
        floor, ceiling = max(low, result[-1] - step), min(high, result[-1] + step)
        if floor > ceiling + EPS:
            return None
        result.append(_clamp(value, floor, ceiling))
    return result


def _simplify(
    points: list[tuple[float, float]], tolerance: float
) -> list[tuple[float, float]]:
    """Ramer-Douglas-Peucker on (time, position), measured in position only."""
    if len(points) < 3:
        return points
    (t0, x0), (t1, x1) = points[0], points[-1]
    worst, split = 0.0, 0
    for index in range(1, len(points) - 1):
        t, x = points[index]
        ratio = (t - t0) / (t1 - t0) if t1 > t0 else 0.0
        error = abs(x - (x0 + (x1 - x0) * ratio))
        if error > worst:
            worst, split = error, index
    if worst <= tolerance:
        return [points[0], points[-1]]
    head = _simplify(points[: split + 1], tolerance)
    return head[:-1] + _simplify(points[split:], tolerance)


def _contained(
    path: list[tuple[float, float]],
    grid: list[float],
    lows: list[float],
    highs: list[float],
) -> bool:
    for t, low, high in zip(grid, lows, highs, strict=True):
        x = _interpolate_path(path, t)
        if x < low - 1e-6 or x > high + 1e-6:
            return False
    return True


def _interpolate_path(path: Sequence[tuple[float, float]], t: float) -> float:
    if t <= path[0][0]:
        return path[0][1]
    for (t0, x0), (t1, x1) in zip(path, path[1:], strict=False):
        if t <= t1:
            ratio = (t - t0) / (t1 - t0) if t1 > t0 else 1.0
            return x0 + (x1 - x0) * ratio
    return path[-1][1]


def path_position(plan: ShotPlan, t: float, config: CameraConfig) -> float:
    if plan.layout == "track":
        return _interpolate_path(plan.path, t)
    return center_left(config)


def plan_shot(
    samples: Sequence[Sample], start: float, end: float, config: CameraConfig
) -> ShotPlan:
    def fallback(reason: str) -> ShotPlan:
        return ShotPlan(start, end, config.fallback, reason)

    if not samples:
        return fallback("no_samples")
    spans = [(sample.t, _union(sample.faces, config)) for sample in samples]
    seen = [(t, span) for t, span in spans if span is not None]
    if len(seen) < MIN_COVERAGE * len(samples):
        return fallback("no_reliable_face")
    anchors: list[tuple[float, float, float]] = []
    for t, span in seen:
        low, high = _interval(span, config)
        if low > high + EPS:
            return fallback("faces_wider_than_crop")
        anchors.append((t, low, max(low, high)))
    grid = _grid(start, end, [t for t, _, _ in anchors])
    bounds = [_interpolate(anchors, t) for t in grid]
    lows, highs = [low for low, _ in bounds], [high for _, high in bounds]
    center = center_left(config)
    if all(low - EPS <= center <= high + EPS for low, high in bounds):
        return ShotPlan(start, end, "center", "center_contains_faces")
    low_all, high_all = max(lows), min(highs)
    if low_all <= high_all + EPS:
        position = _clamp(center, low_all, high_all)
        return ShotPlan(
            start,
            end,
            "track",
            "static_face_crop",
            ((start, position), (end, position)),
        )
    steps = [MAX_SPEED * (t1 - t0) for t0, t1 in zip(grid, grid[1:], strict=False)]
    reach = _reachable(lows, highs, steps)
    if reach is None:
        return fallback("unstable_face_track")
    raw = _follow(reach, steps, center)
    path = _limit(_smooth(raw, lows, highs), lows, highs, steps) or raw
    points = list(zip(grid, path, strict=True))
    for tolerance in (SIMPLIFY_TOLERANCE, SIMPLIFY_TOLERANCE / 4, 0.0):
        simple = _simplify(points, tolerance) if tolerance else points
        if _contained(simple, grid, lows, highs):
            return ShotPlan(start, end, "track", "dynamic_face_track", tuple(simple))
    return fallback("unstable_face_track")


def split_shots(cuts: Sequence[float], duration: float) -> list[tuple[float, float]]:
    """Shot boundaries, dropping cuts that would leave a sliver of a shot."""
    edges = [0.0]
    for cut in sorted(cuts):
        if cut - edges[-1] >= MIN_SHOT_SECONDS and duration - cut >= MIN_SHOT_SECONDS:
            edges.append(cut)
    edges.append(duration)
    return list(zip(edges, edges[1:], strict=False))


def plan_clip(
    samples: Sequence[Sample],
    cuts: Sequence[float],
    duration: float,
    config: CameraConfig,
) -> ReframePlan:
    shots = []
    for start, end in split_shots(cuts, duration):
        inside = [s for s in samples if start - EPS <= s.t < end - EPS]
        shots.append(plan_shot(inside, start, end, config))
    return ReframePlan(tuple(shots))
