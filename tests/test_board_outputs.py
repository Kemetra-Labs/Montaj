from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from montaj import check, engine
from montaj.board_view import OUTPUT_SIZE, check_note, recent_outputs
from montaj.config import Settings
from montaj.db import connect, insert
from montaj.models import CampaignRules, CheckResult, Clip, ClipStatus, Niche, Source
from tests.dashboard_helpers import _add_source


def _clip(db_path: Path, clip_id: int, status: ClipStatus, **over: Any) -> None:
    with connect(db_path) as db:
        insert(
            db,
            Clip(id=clip_id, source_id=7, start_s=0, end_s=42, status=status, **over),
        )


def _outputs(db_path: Path, limit: int = 5) -> tuple[Any, ...]:
    return recent_outputs(Settings(data_dir=db_path.parent), limit)


def test_recent_outputs_newest_first_and_skips_unrendered(db_path: Path) -> None:
    _add_source(db_path)
    for clip_id, status in enumerate(
        (
            ClipStatus.CANDIDATE,
            ClipStatus.RANKED,
            ClipStatus.REJECTED,
            ClipStatus.CHECKED,
            ClipStatus.NEEDS_FIX,
            ClipStatus.APPROVED,
        ),
        start=1,
    ):
        _clip(db_path, clip_id, status)
    assert [record.clip_id for record in _outputs(db_path)] == [6, 5, 4]
    assert [record.clip_id for record in _outputs(db_path, limit=2)] == [6, 5]


def test_record_fields(db_path: Path) -> None:
    _add_source(db_path)
    media = db_path.parent / "clips/4/final.mp4"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"fixture")
    _clip(
        db_path,
        4,
        ClipStatus.CHECKED,
        hook_text="Why the first three seconds matter",
        final_path="clips/4/final.mp4",
    )
    (record,) = _outputs(db_path)
    assert record.title == "Why the first three seconds matter"
    assert (record.source_id, record.file, record.duration_s) == (7, "final.mp4", 42.0)
    assert record.status is ClipStatus.CHECKED
    assert record.check_failed is False and record.check_note == ""
    assert record.blocked_reason == ""
    assert isinstance(record.finished_at, datetime)


def test_failed_record_carries_a_fixed_note(db_path: Path) -> None:
    _add_source(db_path)
    _clip(
        db_path,
        5,
        ClipStatus.NEEDS_FIX,
        check_result=CheckResult.FAIL,
        decision_reason=(
            "Length 75.00s is outside 20.00-60.00s.; Matches banned word or topic: x"
        ),
    )
    _clip(db_path, 6, ClipStatus.CHECKED, check_result=CheckResult.PASS)
    by_id = {record.clip_id: record for record in _outputs(db_path)}
    assert by_id[5].check_failed is True
    assert by_id[5].check_note == (
        "Length is outside the allowed range. Uses a banned word or topic."
    )
    assert by_id[6].check_note == ""


def test_ranking_reason_is_not_a_check_reason(db_path: Path) -> None:
    _add_source(db_path)
    _clip(
        db_path,
        4,
        ClipStatus.CHECKED,
        check_result=CheckResult.PASS,
        decision_reason="Strong opening line",
    )
    (record,) = _outputs(db_path)
    assert record.check_failed is False and record.check_note == ""


def test_missing_title_media_and_outside_path(db_path: Path) -> None:
    _add_source(db_path)
    outside = db_path.parent.parent / "escape.mp4"
    outside.write_bytes(b"fixture")
    _clip(db_path, 4, ClipStatus.CHECKED)
    _clip(db_path, 5, ClipStatus.CHECKED, final_path="../escape.mp4")
    by_id = {record.clip_id: record for record in _outputs(db_path)}
    assert by_id[4].title == "Clip 4" and by_id[4].file == ""
    assert by_id[4].finished_at is None
    assert by_id[5].finished_at is None


def test_empty_database(db_path: Path) -> None:
    assert _outputs(db_path) == ()


_UNKNOWN = "Open the clip preview to see why."


@pytest.mark.parametrize(
    ("reason", "note"),
    [
        (
            "Length 75.00s is outside 20.00-60.00s.",
            "Length is outside the allowed range.",
        ),
        ("Missing required hashtag: #ad", "A required hashtag is missing."),
        ("Missing disclosure: Paid", "The disclosure is missing."),
        ("Matches banned word or topic: x", "Uses a banned word or topic."),
        ("Source is not permitted.", "The source is not permitted."),
        ("This clip was already posted.", "This clip was already posted."),
        (
            "No matching account to apply the daily post cap.",
            "No account matches the daily post cap.",
        ),
        (
            "Possible medical claim or promise: cure",
            "Possible medical claim or promise.",
        ),
        ("Strong opening line", _UNKNOWN),
        ("Traceback (most recent call last)", _UNKNOWN),
        ("<script>x</script>", _UNKNOWN),
        (
            "Missing disclosure: Paid; Source is not permitted.",
            "The disclosure is missing. The source is not permitted.",
        ),
        ("Strong; Weak", _UNKNOWN),
        (None, ""),
        ("", ""),
    ],
)
def test_check_note_uses_fixed_wording(reason: str | None, note: str) -> None:
    assert check_note(reason) == note


def test_record_carries_the_permission_reason(db_path: Path) -> None:
    with connect(db_path) as db:
        for source_id, allowed, basis in (
            (7, True, "Creator approval"),
            (8, False, None),
            (9, True, " "),
        ):
            insert(
                db,
                Source(
                    id=source_id,
                    path=f"s{source_id}.mp4",
                    title="T",
                    niche=Niche.HEALTH,
                    source_allowed=allowed,
                    allowed_basis=basis,
                ),
            )
            insert(
                db,
                Clip(
                    id=source_id,
                    source_id=source_id,
                    start_s=0,
                    end_s=5,
                    status=ClipStatus.CHECKED,
                ),
            )
    by_source = {
        record.source_id: record.blocked_reason for record in _outputs(db_path)
    }
    assert by_source == {
        7: "",
        8: "Record permission for this source first.",
        9: "Record a permission basis for this source first.",
    }


def test_odd_media_paths_give_a_record_without_an_age(db_path: Path) -> None:
    _add_source(db_path)
    _clip(db_path, 4, ClipStatus.CHECKED, final_path="a\x00b.mp4")
    (record,) = _outputs(db_path)
    assert record.finished_at is None


def test_blank_hook_text_falls_back_to_hook_a(db_path: Path) -> None:
    _add_source(db_path)
    _clip(db_path, 4, ClipStatus.CHECKED, hook_text="  ", hook_a="Hook A")
    (record,) = _outputs(db_path)
    assert record.title == "Hook A"


def test_limit_must_be_positive(db_path: Path) -> None:
    with pytest.raises(ValueError):
        _outputs(db_path, limit=0)


def test_output_size_matches_the_cut_styles() -> None:
    assert "1080x1920" in engine.CUT_STYLE and "1080x1920" in engine.CUT_STYLE_V2
    assert OUTPUT_SIZE == "1080×1920"


def test_record_knows_whether_the_preview_lists_the_clip(db_path: Path) -> None:
    _add_source(db_path)
    _clip(db_path, 4, ClipStatus.CHECKED, final_path="clips/4/final.mp4")
    _clip(db_path, 5, ClipStatus.RENDERED, raw_path="clips/5/raw.mp4")
    _clip(db_path, 6, ClipStatus.POSTED, final_path="clips/6/final.mp4")
    by_id = {record.clip_id: record.in_preview for record in _outputs(db_path)}
    assert by_id == {4: True, 5: False, 6: False}


def test_check_note_handles_reasons_that_contain_the_separator() -> None:
    cap = check.daily_cap_failure(
        CampaignRules(min_seconds=20, max_seconds=60, max_posts_per_day=3), 3
    )
    assert cap is not None
    assert check_note(cap.reason) == "The daily post cap is reached."
    length = "Length 75.00s is outside 20.00-60.00s."
    assert check_note(f"{length}; {cap.reason}") == (
        "Length is outside the allowed range. The daily post cap is reached."
    )
    assert (
        check_note("Matches banned word or topic: a; b")
        == "Uses a banned word or topic."
    )
    assert check_note("Strong opening; Length 75.00s is outside 20.00-60.00s.") == (
        "Open the clip preview to see why. Length is outside the allowed range."
    )
