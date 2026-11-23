from pathlib import Path

import pytest

from montaj.board import CellState, NextAction, build_rows
from montaj.config import Settings
from montaj.db import connect, insert
from montaj.models import Clip, ClipStatus, Niche, Source

_BLOCKED_REASON = "Record permission for this source first."
_NO_BASIS_REASON = "Record a permission basis for this source first."
_CHECK_FAILED = "Check has failures. Fix the clips first."
_META = '{"title":"Interview","duration":18,"video_hash":"v","audio_hash":"a"}'


def _add_source(db_path: Path, **fields: object) -> Settings:
    settings = Settings(data_dir=db_path.parent)
    values: dict[str, object] = {
        "id": 1,
        "path": "input.mp4",
        "title": "Interview",
        "niche": Niche.HEALTH,
        "source_allowed": True,
        "allowed_basis": "Creator approval",
    }
    with connect(db_path) as db:
        insert(db, Source.model_validate(values | fields))
    return settings


def _add_clip(db_path: Path, status: ClipStatus, clip_id: int = 1) -> None:
    with connect(db_path) as db:
        insert(db, Clip(id=clip_id, source_id=1, start_s=0, end_s=5, status=status))


def _imported(settings: Settings, *extra: str) -> None:
    directory = settings.data_dir / "sources/1"
    directory.mkdir(parents=True, exist_ok=True)
    for name in ("video.mp4", "audio.wav", *extra):
        (directory / name).write_text("x", encoding="utf-8")
    (directory / "meta.json").write_text(_META, encoding="utf-8")


def _ranked(settings: Settings) -> None:
    _imported(settings, "transcript.json", "candidates.json", "rank.json")


def _states(settings: Settings) -> dict[str, CellState]:
    (row,) = build_rows(settings, running=None)
    return {cell.key: cell.state for cell in row.cells}


def test_new_source_has_only_ingest_pending(db_path: Path) -> None:
    settings = _add_source(db_path)
    (row,) = build_rows(settings, running=None)
    first, *rest = row.cells
    assert (first.key, first.state) == ("ingest", CellState.PENDING)
    assert [cell.key for cell in rest] == [
        "transcribe",
        "rank",
        "render",
        "draft",
        "review",
        "export",
    ]
    assert all(cell.state is CellState.BLOCKED for cell in rest)
    assert all(cell.reason == "Ingest first" for cell in rest)
    assert row.action == NextAction("resume", "Resume")


def test_saved_checkpoints_mark_transcribe_and_rank_done(db_path: Path) -> None:
    settings = _add_source(db_path)
    _ranked(settings)
    states = _states(settings)
    assert states["ingest"] is CellState.DONE
    assert states["transcribe"] is CellState.DONE
    assert states["rank"] is CellState.DONE
    assert states["render"] is CellState.PENDING
    assert states["draft"] is CellState.BLOCKED


def test_candidates_without_ranking_leave_rank_pending(db_path: Path) -> None:
    settings = _add_source(db_path)
    _imported(settings, "transcript.json", "candidates.json")
    states = _states(settings)
    assert states["transcribe"] is CellState.DONE
    assert states["rank"] is CellState.PENDING


def test_unpermitted_source_blocks_everything(db_path: Path) -> None:
    settings = _add_source(db_path, source_allowed=False)
    (row,) = build_rows(settings, running=(1, "render"))
    assert all(cell.state is CellState.BLOCKED for cell in row.cells)
    assert all(cell.reason == _BLOCKED_REASON for cell in row.cells)
    assert row.action is None


@pytest.mark.parametrize("basis", ["", "   ", None])
def test_blank_basis_blocks_every_cell_without_an_action(
    db_path: Path, basis: str | None
) -> None:
    settings = _add_source(db_path, allowed_basis=basis)
    _ranked(settings)
    _add_clip(db_path, ClipStatus.CHECKED)
    (row,) = build_rows(settings, running=None)
    assert all(cell.state is CellState.BLOCKED for cell in row.cells)
    assert all(cell.reason == _NO_BASIS_REASON for cell in row.cells)
    assert row.action is None


def test_failed_check_blocks_review_export_with_reason(db_path: Path) -> None:
    settings = _add_source(db_path)
    _ranked(settings)
    _add_clip(db_path, ClipStatus.NEEDS_FIX)
    (row,) = build_rows(settings, running=None)
    cells = {cell.key: cell for cell in row.cells}
    assert cells["draft"].state is CellState.FAILED
    for key in ("review", "export"):
        assert cells[key].state is CellState.BLOCKED
        assert cells[key].reason == _CHECK_FAILED
    assert row.action is not None and row.action.kind == "fix"


def test_failed_check_with_a_checked_clip_still_lets_review_open(
    db_path: Path,
) -> None:
    settings = _add_source(db_path)
    _ranked(settings)
    _add_clip(db_path, ClipStatus.NEEDS_FIX)
    _add_clip(db_path, ClipStatus.CHECKED, clip_id=2)
    (row,) = build_rows(settings, running=None)
    cells = {cell.key: cell for cell in row.cells}
    assert cells["export"].reason == _CHECK_FAILED
    assert cells["review"].reason != _CHECK_FAILED
    assert row.action is not None and row.action.kind == "fix"


def test_exported_stage_stays_done_beside_a_failed_check(db_path: Path) -> None:
    settings = _add_source(db_path)
    _ranked(settings)
    _add_clip(db_path, ClipStatus.EXPORTED)
    _add_clip(db_path, ClipStatus.NEEDS_FIX, clip_id=2)
    states = _states(settings)
    assert states["export"] is CellState.DONE and states["review"] is CellState.DONE


def test_running_stage_is_marked_running(db_path: Path) -> None:
    settings = _add_source(db_path)
    _ranked(settings)
    (row,) = build_rows(settings, running=(1, "render"))
    states = {cell.key: cell.state for cell in row.cells}
    assert states["render"] is CellState.RUNNING
    assert states["rank"] is CellState.DONE
    assert states["draft"] is CellState.BLOCKED
    (other,) = build_rows(settings, running=(2, "render"))
    other_states = {cell.key: cell.state for cell in other.cells}
    assert other_states["render"] is CellState.PENDING


def test_checked_clips_offer_review_action(db_path: Path) -> None:
    settings = _add_source(db_path)
    _ranked(settings)
    _add_clip(db_path, ClipStatus.CHECKED)
    (row,) = build_rows(settings, running=None)
    assert row.action == NextAction("review", "Open review")
    states = {cell.key: cell.state for cell in row.cells}
    assert states["render"] is CellState.DONE
    assert states["draft"] is CellState.DONE
    assert states["review"] is CellState.PENDING
    assert states["export"] is CellState.BLOCKED


def test_approved_clip_offers_export_action(db_path: Path) -> None:
    settings = _add_source(db_path)
    _ranked(settings)
    _add_clip(db_path, ClipStatus.APPROVED)
    (row,) = build_rows(settings, running=None)
    assert row.action == NextAction("export", "Export")
    states = {cell.key: cell.state for cell in row.cells}
    assert states["review"] is CellState.DONE
    assert states["export"] is CellState.PENDING


def test_needs_fix_clip_offers_fix_action_and_fails_draft(db_path: Path) -> None:
    settings = _add_source(db_path)
    _ranked(settings)
    _add_clip(db_path, ClipStatus.NEEDS_FIX)
    (row,) = build_rows(settings, running=None)
    assert row.action is not None and row.action.kind == "fix"
    states = {cell.key: cell.state for cell in row.cells}
    assert states["draft"] is CellState.FAILED
    assert states["render"] is CellState.DONE


def test_cut_only_clip_leaves_render_pending(db_path: Path) -> None:
    settings = _add_source(db_path)
    _ranked(settings)
    _add_clip(db_path, ClipStatus.RENDERED)
    assert _states(settings)["render"] is CellState.PENDING


def test_exported_clip_completes_every_clip_stage(db_path: Path) -> None:
    settings = _add_source(db_path)
    _ranked(settings)
    _add_clip(db_path, ClipStatus.EXPORTED)
    (row,) = build_rows(settings, running=None)
    assert all(cell.state is CellState.DONE for cell in row.cells)
    assert row.action is None


def test_speech_failure_marks_transcribe_failed_with_fixed_wording(
    db_path: Path,
) -> None:
    settings = _add_source(
        db_path,
        speech_status="failed",
        failure_note="YouTube requires a supported JavaScript runtime. Cookie: secret",
    )
    _imported(settings)
    (row,) = build_rows(settings, running=None)
    cells = {cell.key: cell for cell in row.cells}
    assert cells["transcribe"].state is CellState.FAILED
    assert row.snapshot.problem is not None
    assert cells["transcribe"].reason == row.snapshot.problem
    assert "secret" not in cells["transcribe"].reason
    assert cells["rank"].state is CellState.BLOCKED
    assert cells["rank"].reason == "Transcribe first"
    assert row.action == NextAction("resume", "Retry")


def test_failed_ingest_offers_retry_action(db_path: Path) -> None:
    settings = _add_source(db_path)
    directory = settings.data_dir / "sources/1"
    directory.mkdir(parents=True)
    (directory / "meta.json").write_text(_META, encoding="utf-8")
    (row,) = build_rows(settings, running=None)
    cells = {cell.key: cell for cell in row.cells}
    assert cells["ingest"].state is CellState.FAILED
    assert cells["transcribe"].state is CellState.BLOCKED
    assert row.action == NextAction("resume", "Retry")


def test_fix_action_outranks_review_when_clips_are_mixed(db_path: Path) -> None:
    settings = _add_source(db_path)
    _ranked(settings)
    _add_clip(db_path, ClipStatus.NEEDS_FIX)
    _add_clip(db_path, ClipStatus.CHECKED, clip_id=2)
    (row,) = build_rows(settings, running=None)
    assert row.action is not None and row.action.kind == "fix"


def test_source_id_limits_rows(db_path: Path) -> None:
    settings = _add_source(db_path)
    rows = build_rows(settings, running=None, source_id=1)
    assert [row.snapshot.source_id for row in rows] == [1]
    assert build_rows(settings, running=None, source_id=9) == []
