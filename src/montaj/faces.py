"""Face detection for smart reframing, with an integrity-checked YuNet model.

OpenCV and the YuNet weight are optional. They are imported and loaded lazily so a
basic install never needs them, and any problem turns into ``VisionUnavailable`` so
the render falls back to the centered crop.
"""

import hashlib
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

from montaj.media import atomic_bytes

# Pinned weight from opencv_zoo (MIT). Montaj never downloads it during a render.
YUNET_FILENAME = "face_detection_yunet_2023mar.onnx"
YUNET_SHA256 = "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"
YUNET_URL = (
    "https://media.githubusercontent.com/media/opencv/opencv_zoo/"
    "f12e12798e8314f7c074a6656816c048dcc95b7a/models/face_detection_yunet/"
    + YUNET_FILENAME
)
MIN_SCORE = 0.7


class VisionUnavailable(RuntimeError):
    """Smart reframing cannot run here. The centered crop is used instead."""


@dataclass(frozen=True, slots=True)
class FaceBox:
    """A face as fractions of the frame, so planning ignores analysis resolution."""

    left: float
    top: float
    width: float
    height: float
    score: float

    @property
    def right(self) -> float:
        return self.left + self.width


class FaceDetector(Protocol):
    def detect(self, frame: Any) -> list[FaceBox]: ...


@lru_cache(maxsize=8)
def _digest(path: str, size: int, mtime_ns: int) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def model_digest(path: Path) -> str | None:
    try:
        stat = path.stat()
        return _digest(str(path), stat.st_size, stat.st_mtime_ns)
    except OSError:
        return None


def check_model(path: Path) -> None:
    digest = model_digest(path)
    if digest is None:
        raise VisionUnavailable(
            f"Face model not found at {path}. Run montaj fetch-model to install it."
        )
    if digest != YUNET_SHA256:
        raise VisionUnavailable(
            f"Face model at {path} does not match the approved YuNet weight."
        )


def fetch_model(
    destination: Path,
    *,
    opener: Callable[[str], Any] = urllib.request.urlopen,
) -> Path:
    """Download the pinned YuNet weight once, on explicit request, and verify it."""
    with opener(YUNET_URL) as response:
        payload: bytes = response.read()
    if hashlib.sha256(payload).hexdigest() != YUNET_SHA256:
        raise VisionUnavailable("The downloaded face model failed its SHA-256 check.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    atomic_bytes(destination, payload)
    return destination


class YuNetDetector:
    """YuNet through OpenCV's FaceDetectorYN. One instance handles one frame size."""

    def __init__(self, model: Path, *, threads: int = 2) -> None:
        check_model(model)
        try:
            import cv2
        except ImportError as exc:
            raise VisionUnavailable(
                "Install the vision extra (uv sync --extra vision)."
            ) from exc
        cv2.setNumThreads(threads)
        self._cv2 = cv2
        self._model = str(model)
        self._detector: Any = None
        self._size: tuple[int, int] | None = None

    def _ensure(self, width: int, height: int) -> Any:
        if self._detector is None or self._size != (width, height):
            try:
                self._detector = self._cv2.FaceDetectorYN.create(
                    self._model, "", (width, height), MIN_SCORE, 0.3, 5000
                )
            except self._cv2.error as exc:
                raise VisionUnavailable(f"OpenCV could not load YuNet: {exc}") from exc
            self._size = (width, height)
        return self._detector

    def detect(self, frame: Any) -> list[FaceBox]:
        height, width = frame.shape[:2]
        _, rows = self._ensure(width, height).detect(frame)
        if rows is None:
            return []
        return [
            FaceBox(
                left=max(0.0, float(row[0]) / width),
                top=max(0.0, float(row[1]) / height),
                width=min(1.0, float(row[2]) / width),
                height=min(1.0, float(row[3]) / height),
                score=float(row[14]),
            )
            for row in rows
        ]
