"""Choose the render recipe and report whether smart reframing can run here.

The recipe string joins the cut fingerprint, so any change to the engine, its
versions, the face model or the loudness policy re-renders unreviewed clips.
Reviewed clips stay pinned to the recipe they were rendered with (see pipeline).
"""

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from montaj.camera import CameraConfig, Layout
from montaj.config import Settings
from montaj.db import connect, fetch
from montaj.faces import YUNET_SHA256, VisionUnavailable, check_model, model_digest
from montaj.loudness import LoudnessPolicy
from montaj.media import atomic_text
from montaj.models import Niche, Source
from montaj.pipeline import LegacyRecipe, input_hash

# The previous centered recipe. Reviewed clips rendered with it stay pinned to it.
CUT_STYLE = "center-1080x1920-h264-aac-30fps-fade015-v1"
CUT_STYLE_V2 = "smart-1080x1920-h264-aac-30fps-fade015-v2"
REFRAME_VERSION = "reframe-v1"
GAMES_MIN_FACE_HEIGHT = 0.18
DEFAULT_MIN_FACE_HEIGHT = 0.07


@dataclass(frozen=True, slots=True)
class VisionStatus:
    ready: bool
    reason: str
    tag: str


@lru_cache(maxsize=8)
def _status(model: str, size: int, mtime_ns: int) -> VisionStatus:
    try:
        import cv2
    except ImportError:
        return VisionStatus(False, "opencv is not installed", "no-opencv")
    try:
        import scenedetect
    except ImportError:
        return VisionStatus(False, "scenedetect is not installed", "no-scenedetect")
    try:
        check_model(Path(model))
    except VisionUnavailable as exc:
        return VisionStatus(False, str(exc), "no-model")
    major_minor = ".".join(str(cv2.__version__).split(".")[:2])
    tag = f"cv{major_minor}-sd{scenedetect.__version__}-yunet{YUNET_SHA256[:8]}"
    return VisionStatus(True, "ready", tag)


def vision_status(model: Path) -> VisionStatus:
    try:
        stat = model.stat()
        return _status(str(model), stat.st_size, stat.st_mtime_ns)
    except OSError:
        return _status(str(model), -1, -1)


def loudness_policy(settings: Settings) -> LoudnessPolicy | None:
    if settings.engine == "legacy" or settings.loudness_target_lufs is None:
        return None
    return LoudnessPolicy(
        settings.loudness_target_lufs,
        settings.loudness_ceiling_dbtp,
        settings.loudness_max_gain_db,
    )


def camera_config(crop_width: float, niche: Niche | None) -> CameraConfig:
    """Screen and game sources keep the whole frame unless a face dominates it."""
    if niche == Niche.GAMES:
        return CameraConfig(crop_width, "fit", GAMES_MIN_FACE_HEIGHT)
    return CameraConfig(crop_width, "center", DEFAULT_MIN_FACE_HEIGHT)


def fallback_layout(niche: Niche | None) -> Layout:
    return "fit" if niche == Niche.GAMES else "center"


def cut_recipe(settings: Settings, niche: Niche | None = None) -> str:
    if settings.engine == "legacy":
        return CUT_STYLE
    status = vision_status(settings.resolved_yunet_path)
    policy = loudness_policy(settings)
    digest = input_hash(
        REFRAME_VERSION,
        status.tag if status.ready else "center-only",
        fallback_layout(niche),
        policy.tag() if policy else "loudness-off",
    )
    return f"{CUT_STYLE_V2}:{digest[:20]}"


def source_niche(settings: Settings, directory: Path) -> Niche | None:
    """Read the niche of data/sources/<id>. Unknown sources get the neutral recipe."""
    try:
        source_id = int(directory.name)
        with connect(settings.db_path) as db:
            source = fetch(db, Source, source_id)
    except (ValueError, OSError, sqlite3.Error):
        return None
    return source.niche if source is not None else None


RECIPE_FILE = "recipe.json"


def _recipe_path(data_dir: Path, clip_id: int) -> Path:
    return data_dir / "clips" / str(clip_id) / RECIPE_FILE


def recipe_lookup(data_dir: Path) -> Callable[[int], LegacyRecipe | None]:
    """Read the recipe each clip was rendered with, so reviewed clips stay pinned."""

    def lookup(clip_id: int) -> LegacyRecipe | None:
        try:
            payload = json.loads(_recipe_path(data_dir, clip_id).read_text("utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(payload, dict) or not isinstance(payload.get("cut"), str):
            return None
        captions = payload.get("captions")
        return LegacyRecipe(
            payload["cut"], captions if isinstance(captions, str) else None
        )

    return lookup


def record_recipe(
    data_dir: Path, clip_id: int, cut: str, captions: str | None = None
) -> None:
    """Remember the recipe a stage used. A new raw render has no captions yet."""
    path = _recipe_path(data_dir, clip_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"cut": cut, "captions": captions}
    atomic_text(path, json.dumps(payload, indent=2) + "\n")


__all__ = ["model_digest", "cut_recipe", "vision_status"]
