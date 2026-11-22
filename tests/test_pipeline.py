from pathlib import Path

import pytest

from montaj.db import connect, fetch, init_db, insert, save_clip
from montaj.models import (
    CheckResult,
    Clip,
    ClipStage,
    ClipStatus,
    Niche,
    Source,
    utc_now,
)
from montaj.pipeline import (
    ClipInputs,
    invalidate_review,
    is_stale,
    next_stage,
    stage_input_hash,
)


@pytest.fixture
def reviewed_clip() -> Clip:
    clip = Clip(
        source_id=1,
        start_s=1,
        end_s=25,
        raw_path="clips/raw.mp4",
        final_path="clips/final.mp4",
        hook_text="The hook",
        caption="The caption",
        status=ClipStatus.APPROVED,
        check_result=CheckResult.PASS,
        reviewed_at=utc_now(),
    )
    clip.input_hashes = {
        stage: stage_input_hash(clip, stage)
        for stage in (ClipStage.CUT, ClipStage.CAPTIONS, ClipStage.DRAFT)
    }
    clip.check_input_hash = stage_input_hash(clip, ClipStage.CHECK)
    clip.reviewed_input_hash = stage_input_hash(clip, ClipStage.REVIEW)
    return clip


def test_plain_rerun_preserves_approval(reviewed_clip: Clip) -> None:
    assert not is_stale(reviewed_clip, ClipStage.CUT)
    assert not is_stale(reviewed_clip, ClipStage.CHECK)
    assert invalidate_review(reviewed_clip).status == ClipStatus.APPROVED
    assert next_stage(reviewed_clip) == ClipStage.EXPORT


def test_trim_invalidates_renders_and_quote_based_draft(reviewed_clip: Clip) -> None:
    trimmed = reviewed_clip.model_copy(update={"start_s": 2.0})
    assert next_stage(trimmed) == ClipStage.CUT
    assert is_stale(trimmed, ClipStage.CAPTIONS)
    assert is_stale(trimmed, ClipStage.DRAFT)
    assert is_stale(trimmed, ClipStage.CHECK)
    assert invalidate_review(trimmed).changed_since_review

    independent = reviewed_clip.model_copy(
        update={"draft_quotes_clip": False}, deep=True
    )
    independent.input_hashes[ClipStage.DRAFT] = stage_input_hash(
        independent, ClipStage.DRAFT
    )
    independent.start_s = 2.0
    assert not is_stale(independent, ClipStage.DRAFT)


def test_hook_edit_rechecks_and_invalidates_approval(reviewed_clip: Clip) -> None:
    edited = reviewed_clip.model_copy(update={"hook_text": "Edited hook"})
    assert next_stage(edited) == ClipStage.CHECK
    assert not is_stale(edited, ClipStage.DRAFT)
    updated = invalidate_review(edited)
    assert updated.status == ClipStatus.DRAFTED
    assert updated.changed_since_review


def test_rule_edit_rechecks_without_erasing_approval(reviewed_clip: Clip) -> None:
    inputs = ClipInputs(rules_hash="v2")
    assert next_stage(reviewed_clip, inputs) == ClipStage.CHECK
    assert invalidate_review(reviewed_clip, inputs).status == ClipStatus.APPROVED


def test_human_rejection_is_preserved(reviewed_clip: Clip) -> None:
    rejected = reviewed_clip.model_copy(update={"status": ClipStatus.REJECTED})
    assert next_stage(rejected, ClipInputs(source_hash="changed")) is None


def test_persist_clip_state(tmp_path: Path, reviewed_clip: Clip) -> None:
    path = tmp_path / "montaj.db"
    init_db(path)
    with connect(path) as db:
        insert(db, Source(id=1, path="source.mp4", title="Source", niche=Niche.GAMES))
        inserted = insert(db, reviewed_clip)
        assert inserted.id is not None
        assert inserted.input_hashes == reviewed_clip.input_hashes
        inserted.hook_text = "New hook"
        updated = invalidate_review(inserted)
        save_clip(db, updated)
        assert fetch(db, Clip, inserted.id) == updated
