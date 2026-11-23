"""The new engine is the default for new renders and never touches reviewed work."""

import shutil
import sys
from pathlib import Path

import pytest

from montaj import captions, engine
from montaj.captions import LEGACY_CAPTION_STYLE, source_inputs
from montaj.config import Settings
from montaj.cut import CUT_STYLE, build_renderer, cut_source, render_inputs
from montaj.db import connect, fetch, save_clip
from montaj.engine import CUT_STYLE_V2, cut_recipe, record_recipe, vision_status
from montaj.media import file_hash
from montaj.models import CheckResult, Clip, ClipStage, ClipStatus, Niche, utc_now
from montaj.pipeline import (
    ClipInputs,
    effective_options,
    input_hash,
    invalidate_review,
    is_stale,
    next_stage,
    stage_input_hash,
)
from montaj.render import RenderReport

FIXTURES = Path(__file__).parent / "fixtures"


class _Writer:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.calls = 0

    def render(
        self, video: Path, output: Path, start: float, end: float
    ) -> RenderReport:
        self.calls += 1
        output.write_bytes(self.payload)
        return RenderReport(engine="test", reason="ok")


def _previous_inputs(settings: Settings) -> ClipInputs:
    """Exactly what the code before the unified engine fingerprinted."""
    directory = settings.data_dir / "sources/1"
    font = settings.resolved_font_path
    return render_inputs(
        directory / "video.mp4",
        file_hash(directory / "transcript.json"),
        {
            ClipStage.CAPTIONS: input_hash(
                LEGACY_CAPTION_STYLE, file_hash(font), "ass-word-highlight-v1"
            )
        },
        style=CUT_STYLE,
        legacy=(),
    )


def _approved_before_upgrade(settings: Settings, clip: Clip) -> Clip:
    directory = settings.data_dir / "sources/1"
    shutil.copyfile(FIXTURES / "transcript.json", directory / "transcript.json")
    old = _previous_inputs(settings)
    raw = settings.data_dir / "clips/1/raw.mp4"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(b"rendered and approved before the upgrade")
    (raw.parent / "final.mp4").write_bytes(b"captioned and approved")
    clip.raw_path, clip.final_path = "clips/1/raw.mp4", "clips/1/final.mp4"
    clip.hook_a, clip.caption = "Hook", "Caption"
    for stage in (ClipStage.CUT, ClipStage.CAPTIONS, ClipStage.DRAFT):
        clip.input_hashes[stage] = stage_input_hash(clip, stage, old)
    clip.check_result = CheckResult.PASS
    clip.check_input_hash = stage_input_hash(clip, ClipStage.CHECK, old)
    clip.reviewed_at = utc_now()
    clip.reviewed_input_hash = stage_input_hash(clip, ClipStage.REVIEW, old)
    clip.status = ClipStatus.APPROVED
    with connect(settings.db_path) as db:
        save_clip(db, clip)
    return clip


def test_new_default_leaves_approved_clips_current_and_untouched(
    prepared_source: Settings, ranked_clip: Clip
) -> None:
    clip = _approved_before_upgrade(prepared_source, ranked_clip)
    current = source_inputs(prepared_source, prepared_source.data_dir / "sources/1")
    assert current.stage_options[ClipStage.CUT].startswith(CUT_STYLE_V2)
    for stage in (ClipStage.CUT, ClipStage.CAPTIONS, ClipStage.CHECK, ClipStage.REVIEW):
        assert not is_stale(clip, stage, current), stage
    assert next_stage(clip, current) == ClipStage.EXPORT
    assert invalidate_review(clip, current).status == ClipStatus.APPROVED

    writer = _Writer(b"new engine")
    assert cut_source(prepared_source, 1, renderer=writer) == (0, 1)
    assert writer.calls == 0
    raw = prepared_source.data_dir / "clips/1/raw.mp4"
    assert raw.read_bytes() == b"rendered and approved before the upgrade"
    with connect(prepared_source.db_path) as db:
        stored = fetch(db, Clip, 1)
    assert stored is not None and stored.status == ClipStatus.APPROVED
    assert not stored.changed_since_review


def test_unreviewed_and_trimmed_clips_move_to_the_new_engine(
    prepared_source: Settings, ranked_clip: Clip
) -> None:
    clip = _approved_before_upgrade(prepared_source, ranked_clip)
    current = source_inputs(prepared_source, prepared_source.data_dir / "sources/1")
    unreviewed = clip.model_copy(
        update={"status": ClipStatus.CAPTIONED, "reviewed_at": None}
    )
    assert next_stage(unreviewed, current) == ClipStage.CUT
    trimmed = clip.model_copy(update={"end_s": clip.end_s - 0.5})
    assert next_stage(trimmed, current) == ClipStage.CUT
    assert invalidate_review(trimmed, current).changed_since_review


def test_explicit_upgrade_rerenders_a_reviewed_clip_and_requires_review(
    prepared_source: Settings, ranked_clip: Clip
) -> None:
    _approved_before_upgrade(prepared_source, ranked_clip)
    writer = _Writer(b"new engine")
    assert cut_source(prepared_source, 1, renderer=writer, upgrade=True) == (1, 0)
    with connect(prepared_source.db_path) as db:
        stored = fetch(db, Clip, 1)
    assert stored is not None
    assert stored.status == ClipStatus.DRAFTED and stored.changed_since_review
    clip_dir = prepared_source.data_dir / "clips/1"
    assert (clip_dir / "raw.mp4").read_bytes() == b"new engine"
    assert '"engine": "test"' in (clip_dir / "render.json").read_text(encoding="utf-8")
    # Once upgraded, a plain rerun keeps the new render.
    assert cut_source(prepared_source, 1, renderer=writer) == (0, 1)


def test_legacy_engine_is_an_internal_rollback_to_the_old_recipe(
    prepared_source: Settings,
) -> None:
    rollback = prepared_source.model_copy(update={"engine": "legacy"})
    assert cut_recipe(rollback) == CUT_STYLE
    renderer = build_renderer(rollback)
    assert renderer.reframer is None and renderer.policy is None


def test_recipe_tracks_vision_availability_niche_and_loudness(
    prepared_source: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    missing = prepared_source.model_copy(
        update={"yunet_model_path": tmp_path / "absent.onnx"}
    )
    assert not vision_status(missing.resolved_yunet_path).ready
    assert build_renderer(missing).reframer is None
    center_only = cut_recipe(missing)
    model = tmp_path / "yunet.onnx"
    model.write_bytes(b"stand-in weight")
    ready = prepared_source.model_copy(update={"yunet_model_path": model})
    monkeypatch.setattr(engine, "check_model", lambda path: None)
    engine._status.cache_clear()
    assert vision_status(model).ready
    assert cut_recipe(ready) != center_only
    assert cut_recipe(ready, Niche.GAMES) != cut_recipe(ready, Niche.BUSINESS)
    assert build_renderer(ready).reframer is not None
    engine._status.cache_clear()


def test_missing_opencv_falls_back_to_the_centered_recipe(
    prepared_source: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setitem(sys.modules, "cv2", None)
    engine._status.cache_clear()
    status = vision_status(tmp_path / "model.onnx")
    assert (status.ready, status.reason) == (False, "opencv is not installed")
    assert build_renderer(prepared_source).reframer is None
    engine._status.cache_clear()


def _approved_under_v2(settings: Settings, clip: Clip) -> Clip:
    """Render, caption and approve a clip with the current default engine."""
    directory = settings.data_dir / "sources/1"
    shutil.copyfile(FIXTURES / "transcript.json", directory / "transcript.json")
    assert cut_source(settings, 1, renderer=_Writer(b"v2 render")) == (1, 0)
    with connect(settings.db_path) as db:
        stored = fetch(db, Clip, clip.id or 1)
    assert stored is not None
    inputs = source_inputs(settings, directory)
    cut_used, caption_used = effective_options(stored, inputs)
    assert cut_used is not None and caption_used is not None
    (settings.data_dir / "clips/1/final.mp4").write_bytes(b"v2 captioned")
    stored.final_path = "clips/1/final.mp4"
    stored.hook_a, stored.caption = "Hook", "Caption"
    for stage in (ClipStage.CAPTIONS, ClipStage.DRAFT):
        stored.input_hashes[stage] = stage_input_hash(stored, stage, inputs)
    record_recipe(settings.data_dir, 1, cut_used, caption_used)
    stored.check_result = CheckResult.PASS
    stored.check_input_hash = stage_input_hash(stored, ClipStage.CHECK, inputs)
    stored.reviewed_at = utc_now()
    stored.reviewed_input_hash = stage_input_hash(stored, ClipStage.REVIEW, inputs)
    stored.status = ClipStatus.APPROVED
    with connect(settings.db_path) as db:
        save_clip(db, stored)
    return stored


@pytest.mark.parametrize(
    "change",
    ["rollback", "loudness", "vision_ready", "caption_style", "opencv_removed"],
)
def test_clips_approved_under_v2_survive_later_setting_changes(
    prepared_source: Settings,
    ranked_clip: Clip,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    change: str,
) -> None:
    clip = _approved_under_v2(prepared_source, ranked_clip)
    settings = prepared_source
    if change == "rollback":
        settings = settings.model_copy(update={"engine": "legacy"})
    elif change == "loudness":
        settings = settings.model_copy(update={"loudness_target_lufs": -20.0})
    elif change == "vision_ready":
        model = tmp_path / "yunet.onnx"
        model.write_bytes(b"weight")
        settings = settings.model_copy(update={"yunet_model_path": model})
        monkeypatch.setattr(engine, "check_model", lambda path: None)
    elif change == "caption_style":
        bigger = captions.load_caption_style().model_copy(update={"size": 96})
        monkeypatch.setattr(captions, "load_caption_style", lambda: bigger)
    else:
        monkeypatch.setitem(sys.modules, "cv2", None)
    engine._status.cache_clear()
    current = source_inputs(settings, settings.data_dir / "sources/1")
    assert current.stage_options != {} and next_stage(clip, current) == ClipStage.EXPORT
    for stage in (ClipStage.CUT, ClipStage.CAPTIONS, ClipStage.CHECK, ClipStage.REVIEW):
        assert not is_stale(clip, stage, current), stage
    writer = _Writer(b"should not render")
    assert cut_source(settings, 1, renderer=writer) == (0, 1) and writer.calls == 0
    raw = settings.data_dir / "clips/1/raw.mp4"
    assert raw.read_bytes() == b"v2 render"
    engine._status.cache_clear()
