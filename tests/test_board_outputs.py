from datetime import datetime
from pathlib import Path
from typing import Any

from montaj.board_view import recent_outputs
from montaj.config import Settings
from montaj.db import connect, insert
from montaj.models import CheckResult, Clip, ClipStatus
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
    assert record.check_failed is False and record.check_reason == ""
    assert isinstance(record.finished_at, datetime)


def test_failed_check_reason_is_shortened(db_path: Path) -> None:
    _add_source(db_path)
    _clip(
        db_path,
        5,
        ClipStatus.NEEDS_FIX,
        check_result=CheckResult.FAIL,
        decision_reason="x" * 400,
    )
    (record,) = _outputs(db_path)
    assert record.check_failed is True
    assert len(record.check_reason) <= 160 and record.check_reason.endswith("...")


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
    assert record.check_failed is False and record.check_reason == ""


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
