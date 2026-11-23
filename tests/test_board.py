from pathlib import Path

import pytest

from montaj.board import CellState, NextAction, build_rows
from montaj.config import Settings
from montaj.db import connect, insert
from montaj.models import Clip, ClipStatus, Niche, Source
from montaj.rank import RankedClip, RankSet

_BLOCKED_REASON = "Record permission for this source first."
_NO_BASIS_REASON = "Record a permission basis for this source first."
_CHECK_FAILED = "Check has failures. Fix the clips first."
_INGEST_STEP = (
    "Ingest this source again from the terminal with montaj ingest, then refresh."
)
_FIX_GUIDANCE = (
    "Fix the flagged clips in the clip preview or with montaj review, "
    "then rerun the check."
)
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


def _rank_path(db_path: Path) -> Path:
    return db_path.parent / "sources/1/rank.json"


def _save_rank(db_path: Path, selected: list[int]) -> None:
    clips = [
        RankedClip(clip_id=clip_id, score=8, reason="r", selected=True)
        for clip_id in selected
    ]
    _rank_path(db_path).write_text(
        RankSet(input_hash="h", clips=clips).model_dump_json(), encoding="utf-8"
    )


def _add_clip(db_path: Path, status: ClipStatus, clip_id: int = 1) -> None:
    """Add a clip and, when a ranking is saved, select it in that ranking."""
    with connect(db_path) as db:
        insert(db, Clip(id=clip_id, source_id=1, start_s=0, end_s=5, status=status))
    path = _rank_path(db_path)
    if path.is_file():
        selected = RankSet.model_validate_json(path.read_bytes()).selected_ids
        _save_rank(db_path, [*selected, clip_id])


def _imported(settings: Settings, *extra: str) -> None:
    directory = settings.data_dir / "sources/1"
    directory.mkdir(parents=True, exist_ok=True)
    for name in ("video.mp4", "audio.wav", *extra):
        (directory / name).write_text("x", encoding="utf-8")
    (directory / "meta.json").write_text(_META, encoding="utf-8")
    if "rank.json" in extra:
        _save_rank(settings.db_path, [])


def _ranked(settings: Settings) -> None:
    _imported(settings, "transcript.json", "candidates.json", "rank.json")


def _states(settings: Settings) -> dict[str, CellState]:
    (row,) = build_rows(settings, running=None)
    return {cell.key: cell.state for cell in row.cells}


def _batch(db_path: Path, statuses: list[ClipStatus]) -> Settings:
    """A ranked source whose working set holds one clip per status, in order."""
    settings = _add_source(db_path)
    _ranked(settings)
    for clip_id, status in enumerate(statuses, start=1):
        _add_clip(db_path, status, clip_id)
    return settings


def test_new_source_has_only_ingest_pending(db_path: Path) -> None:
    settings = _add_source(db_path)
    (row,) = build_rows(settings, running=None)
    first, *rest = row.cells
    assert (first.key, first.state) == ("ingest", CellState.PENDING)
    assert first.reason == _INGEST_STEP
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
    # Resume cannot import a source, so the board offers no button for it.
    assert row.action is None


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
    assert cells["review"].state is CellState.PENDING
    assert cells["export"].reason != _CHECK_FAILED
    assert cells["review"].reason != _CHECK_FAILED
    assert row.action == NextAction("review", "Open review")


def test_flagged_clip_does_not_block_export_of_approved_clips(db_path: Path) -> None:
    settings = _batch(db_path, [ClipStatus.NEEDS_FIX, ClipStatus.APPROVED])
    (row,) = build_rows(settings, running=None)
    cells = {cell.key: cell for cell in row.cells}
    assert cells["export"].state is CellState.PENDING
    assert cells["export"].reason != _CHECK_FAILED
    assert row.action == NextAction("export", "Export")


def test_partly_exported_batch_with_a_flagged_clip_is_not_done(
    db_path: Path,
) -> None:
    # One flagged clip and nothing left to review or export: the check blocks
    # both stages, and an exported clip alone does not make them done.
    settings = _batch(db_path, [ClipStatus.EXPORTED, ClipStatus.NEEDS_FIX])
    (row,) = build_rows(settings, running=None)
    cells = {cell.key: cell for cell in row.cells}
    for key in ("review", "export"):
        assert cells[key].state is CellState.BLOCKED
        assert cells[key].reason == _CHECK_FAILED


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


def test_running_batch_marks_the_first_unfinished_stage_running(
    db_path: Path,
) -> None:
    settings = _add_source(db_path)
    _imported(settings)
    states = {
        c.key: c.state for c in build_rows(settings, running=(1, "batch"))[0].cells
    }
    assert states["ingest"] is CellState.DONE
    assert states["transcribe"] is CellState.RUNNING
    assert states["rank"] is CellState.BLOCKED
    _ranked(settings)
    (row,) = build_rows(settings, running=(1, "batch"))
    states = {cell.key: cell.state for cell in row.cells}
    assert states["rank"] is CellState.DONE
    assert states["render"] is CellState.RUNNING
    (other,) = build_rows(settings, running=(2, "batch"))
    assert all(cell.state is not CellState.RUNNING for cell in other.cells)


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
    assert row.action == NextAction("fix", "Review flagged clips")
    cells = {cell.key: cell for cell in row.cells}
    assert cells["draft"].state is CellState.FAILED
    assert cells["draft"].reason == f"1 flagged clip. {_FIX_GUIDANCE}"
    # A rerun of draft and check on unchanged clips changes nothing.
    assert not cells["draft"].actionable
    assert cells["render"].state is CellState.DONE


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


def test_partial_render_is_pending_with_a_count_and_offers_resume(
    db_path: Path,
) -> None:
    settings = _batch(db_path, [ClipStatus.CAPTIONED] * 3 + [ClipStatus.RANKED] * 7)
    (row,) = build_rows(settings, running=None)
    cells = {cell.key: cell for cell in row.cells}
    assert cells["render"].state is CellState.PENDING
    assert cells["render"].reason == "3 of 10 done"
    assert cells["draft"].state is CellState.BLOCKED
    assert row.action == NextAction("resume", "Resume")


def test_only_the_default_target_counts_toward_done(db_path: Path) -> None:
    statuses = [ClipStatus.CAPTIONED] * 10 + [ClipStatus.RANKED] * 2
    settings = _batch(db_path, statuses)
    assert _states(settings)["render"] is CellState.DONE


def test_review_decisions_count_as_done_for_render_and_draft(
    db_path: Path,
) -> None:
    settings = _batch(
        db_path, [ClipStatus.CHECKED, ClipStatus.REJECTED, ClipStatus.APPROVED]
    )
    states = _states(settings)
    assert states["render"] is CellState.DONE
    assert states["draft"] is CellState.DONE
    assert states["review"] is CellState.PENDING


def test_a_new_ranking_is_not_up_to_date_for_old_renders(db_path: Path) -> None:
    settings = _batch(db_path, [ClipStatus.CAPTIONED, ClipStatus.CAPTIONED])
    assert _states(settings)["render"] is CellState.DONE
    for clip_id in (3, 4):
        with connect(db_path) as db:
            insert(
                db,
                Clip(
                    id=clip_id,
                    source_id=1,
                    start_s=0,
                    end_s=5,
                    status=ClipStatus.RANKED,
                ),
            )
    _save_rank(db_path, [3, 4])
    (row,) = build_rows(settings, running=None)
    cells = {cell.key: cell for cell in row.cells}
    assert cells["render"].state is CellState.PENDING
    assert cells["render"].reason == ""
    assert row.action == NextAction("resume", "Resume")


def test_review_opens_while_other_clips_are_still_rendering(db_path: Path) -> None:
    settings = _batch(db_path, [ClipStatus.CHECKED] + [ClipStatus.RANKED] * 9)
    (row,) = build_rows(settings, running=None)
    cells = {cell.key: cell for cell in row.cells}
    assert cells["render"].state is CellState.PENDING
    assert cells["render"].reason == "1 of 10 done"
    assert cells["review"].state is CellState.PENDING
    assert row.action == NextAction("review", "Open review")


def test_partial_draft_with_a_flagged_clip_stays_runnable(db_path: Path) -> None:
    settings = _batch(
        db_path, [ClipStatus.CHECKED, ClipStatus.NEEDS_FIX, ClipStatus.CAPTIONED]
    )
    (row,) = build_rows(settings, running=None)
    draft = {cell.key: cell for cell in row.cells}["draft"]
    assert draft.state is CellState.PENDING and draft.actionable
    assert draft.reason == f"1 of 3 done, 1 flagged clip. {_FIX_GUIDANCE}"


def test_unreadable_ranking_never_marks_a_clip_stage_done(db_path: Path) -> None:
    settings = _batch(db_path, [ClipStatus.EXPORTED])
    _rank_path(db_path).write_text("not json", encoding="utf-8")
    states = _states(settings)
    for key in ("render", "draft", "review", "export"):
        assert states[key] is not CellState.DONE


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


@pytest.mark.parametrize("status", ["language_mismatch", "no_usable_speech"])
def test_speech_language_failures_offer_no_retry(db_path: Path, status: str) -> None:
    settings = _add_source(db_path, speech_status=status)
    _imported(settings, "transcript.json")
    (row,) = build_rows(settings, running=None)
    transcribe = {cell.key: cell for cell in row.cells}["transcribe"]
    assert transcribe.state is CellState.FAILED
    assert transcribe.reason == row.snapshot.problem
    assert not transcribe.actionable
    assert row.action is None


def test_failed_ingest_points_to_the_terminal(db_path: Path) -> None:
    settings = _add_source(db_path)
    directory = settings.data_dir / "sources/1"
    directory.mkdir(parents=True)
    (directory / "meta.json").write_text(_META, encoding="utf-8")
    (row,) = build_rows(settings, running=None)
    cells = {cell.key: cell for cell in row.cells}
    assert cells["ingest"].state is CellState.FAILED
    assert cells["ingest"].reason == f"Media missing. {_INGEST_STEP}"
    assert cells["transcribe"].state is CellState.BLOCKED
    assert row.action is None


def test_download_failure_belongs_to_the_ingest_stage(db_path: Path) -> None:
    settings = _add_source(
        db_path,
        speech_status="failed",
        failure_note="Requested format is not available. https://signed.example/x",
    )
    (row,) = build_rows(settings, running=None)
    cells = {cell.key: cell for cell in row.cells}
    problem = row.snapshot.problem
    assert problem is not None and "signed" not in problem
    assert cells["ingest"].state is CellState.FAILED
    assert cells["ingest"].reason == f"{problem} {_INGEST_STEP}"
    assert cells["transcribe"].state is CellState.BLOCKED
    assert cells["transcribe"].reason == "Ingest first"
    assert row.action is None


@pytest.mark.parametrize(
    ("statuses", "action"),
    [
        (
            [ClipStatus.NEEDS_FIX, ClipStatus.APPROVED, ClipStatus.CHECKED],
            NextAction("review", "Open review"),
        ),
        (
            [ClipStatus.NEEDS_FIX, ClipStatus.APPROVED],
            NextAction("export", "Export"),
        ),
        ([ClipStatus.NEEDS_FIX], NextAction("fix", "Review flagged clips")),
    ],
)
def test_review_outranks_export_which_outranks_fix(
    db_path: Path, statuses: list[ClipStatus], action: NextAction
) -> None:
    settings = _batch(db_path, statuses)
    (row,) = build_rows(settings, running=None)
    assert row.action == action


def test_source_id_limits_rows(db_path: Path) -> None:
    settings = _add_source(db_path)
    rows = build_rows(settings, running=None, source_id=1)
    assert [row.snapshot.source_id for row in rows] == [1]
    assert build_rows(settings, running=None, source_id=9) == []
