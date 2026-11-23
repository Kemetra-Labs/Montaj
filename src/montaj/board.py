"""Stage progress cells and next actions for the production board."""

from collections import Counter
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Literal

from montaj.config import Settings
from montaj.dashboard import SourceSnapshot, _load
from montaj.dashboard_parts import permission_reason
from montaj.db import connect
from montaj.ingest import source_dir
from montaj.job import DEFAULT_TARGET
from montaj.models import ClipStatus
from montaj.rank import load_rank


class CellState(StrEnum):
    DONE = "done"
    RUNNING = "running"
    BLOCKED = "blocked"
    FAILED = "failed"
    PENDING = "pending"


@dataclass(frozen=True, slots=True)
class StageCell:
    key: str
    label: str
    state: CellState
    reason: str = ""
    # False when running the stage again from the board cannot fix it.
    actionable: bool = True


@dataclass(frozen=True, slots=True)
class NextAction:
    kind: Literal["resume", "review", "export", "fix"]
    label: str


@dataclass(frozen=True, slots=True)
class BoardRow:
    snapshot: SourceSnapshot
    cells: tuple[StageCell, ...]
    action: NextAction | None


@dataclass(frozen=True, slots=True)
class _Batch:
    """Clip statuses of the batch working set. `known` is False when unreadable."""

    statuses: Counter[ClipStatus]
    total: int
    known: bool = True


STRIP: tuple[tuple[str, str], ...] = (
    ("ingest", "Ingest"),
    ("transcribe", "Transcribe"),
    ("rank", "Candidates + rank"),
    ("render", "Cut + captions"),
    ("draft", "Draft + check"),
    ("review", "Review"),
    ("export", "Export"),
)

_CHECK_FAILED = "Check has failures. Fix the clips first."
INGEST_STEP = (
    "Ingest this source again from the terminal with montaj ingest, then refresh."
)
FIX_GUIDANCE = (
    "Fix the flagged clips in the clip preview or with montaj review, "
    "then rerun the check."
)
_OPEN = {CellState.PENDING, CellState.BLOCKED}
_RUNNABLE = {"transcribe", "rank", "render", "draft"}
_MEDIA_FAILURES = {"Media missing", "Metadata unreadable", "Media unavailable"}
# Re-running transcription does not change these recorded outcomes.
_FINAL_SPEECH = {"language_mismatch", "no_usable_speech"}
_DECIDED = {
    ClipStatus.APPROVED,
    ClipStatus.EXPORTED,
    ClipStatus.POSTED,
    ClipStatus.REJECTED,
}
# A clip-stage cell is done once every working-set clip holds a status in its set.
_CLIP_DONE = {
    "render": {
        ClipStatus.CAPTIONED,
        ClipStatus.DRAFTED,
        ClipStatus.CHECKED,
        ClipStatus.NEEDS_FIX,
    }
    | _DECIDED,
    "draft": {ClipStatus.CHECKED} | _DECIDED,
    "review": _DECIDED,
    "export": {ClipStatus.EXPORTED, ClipStatus.POSTED, ClipStatus.REJECTED},
}
# Review opens with any checked clip, export with any approved one.
_OPENS = {"review": ClipStatus.CHECKED, "export": ClipStatus.APPROVED}

_Evidence = tuple[CellState, str, bool]


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def _saved(snapshot: SourceSnapshot, label: str) -> bool:
    return dict(snapshot.checkpoints).get(label) == "Saved"


def _ingest(snapshot: SourceSnapshot) -> _Evidence:
    """A source that is not imported is fixed from the terminal, not the board."""
    if snapshot.media == "Imported":
        return CellState.DONE, "", True
    if snapshot.problem:
        return CellState.FAILED, f"{snapshot.problem} {INGEST_STEP}", False
    if snapshot.media in _MEDIA_FAILURES:
        return CellState.FAILED, f"{snapshot.media}. {INGEST_STEP}", False
    return CellState.PENDING, INGEST_STEP, False


def _transcribe(snapshot: SourceSnapshot) -> _Evidence | None:
    if snapshot.problem and snapshot.media == "Imported":
        retry = snapshot.speech_status not in _FINAL_SPEECH
        return CellState.FAILED, snapshot.problem, retry
    if _saved(snapshot, "Transcript"):
        return CellState.DONE, "", True
    return None


def _draft_flagged(done: int, flagged: int, total: int) -> _Evidence | None:
    """Flagged clips fail the draft cell once nothing else is left to draft.

    While other clips still need drafting, Run stays for them, never Retry.
    """
    if not flagged:
        return None
    flags = f"{_plural(flagged, 'flagged clip')}. {FIX_GUIDANCE}"
    if done + flagged == total:
        return CellState.FAILED, flags, False
    note = f"{done} of {total} done, " if done else ""
    return CellState.PENDING, f"{note}{flags}", True


def _clip_stage(snapshot: SourceSnapshot, key: str, batch: _Batch) -> _Evidence | None:
    statuses = batch.statuses
    done = sum(statuses[status] for status in _CLIP_DONE[key])
    if batch.known and batch.total and done == batch.total:
        return CellState.DONE, "", True
    if key == "draft":
        flagged = _draft_flagged(done, statuses[ClipStatus.NEEDS_FIX], batch.total)
        if flagged is not None:
            return flagged
    note = f"{done} of {batch.total} done" if done and batch.known else ""
    if note or (key in _OPENS and snapshot.clips[_OPENS[key]]):
        return CellState.PENDING, note, True
    return None


def _evidence(snapshot: SourceSnapshot, key: str, batch: _Batch) -> _Evidence | None:
    """A state for a stage that has run or can be judged, else None."""
    if key == "ingest":
        return _ingest(snapshot)
    if key == "transcribe":
        return _transcribe(snapshot)
    if key == "rank":
        if _saved(snapshot, "Candidates") and _saved(snapshot, "Ranking"):
            return CellState.DONE, "", True
        return None
    return _clip_stage(snapshot, key, batch)


def _cells(
    snapshot: SourceSnapshot, batch: _Batch, running: str | None
) -> tuple[StageCell, ...]:
    cells: list[StageCell] = []
    first_open: str | None = None
    for key, label in STRIP:
        found = _evidence(snapshot, key, batch)
        if key == running:
            cell = StageCell(key, label, CellState.RUNNING)
        elif found is not None:
            cell = StageCell(key, label, *found)
        elif first_open is not None:
            cell = StageCell(key, label, CellState.BLOCKED, f"{first_open} first")
        else:
            cell = StageCell(key, label, CellState.PENDING)
        if first_open is None and cell.state is not CellState.DONE:
            first_open = label
        cells.append(cell)
    return tuple(cells)


def _batch_running(cells: tuple[StageCell, ...]) -> tuple[StageCell, ...]:
    """A batch job runs the first unfinished runnable stage."""
    for index, cell in enumerate(cells):
        if cell.key in _RUNNABLE and cell.state is not CellState.DONE:
            running = replace(cell, state=CellState.RUNNING, reason="")
            return (*cells[:index], running, *cells[index + 1 :])
    return cells


def _blocked_cells(reason: str) -> tuple[StageCell, ...]:
    return tuple(
        StageCell(key, label, CellState.BLOCKED, reason) for key, label in STRIP
    )


def _with_check_failures(
    snapshot: SourceSnapshot, cells: tuple[StageCell, ...]
) -> tuple[StageCell, ...]:
    """Flagged clips block review and export only when no other clip can use them."""
    clips = snapshot.clips
    usable = clips[ClipStatus.CHECKED] or clips[ClipStatus.APPROVED]
    if not clips[ClipStatus.NEEDS_FIX] or usable:
        return cells
    return tuple(
        replace(cell, state=CellState.BLOCKED, reason=_CHECK_FAILED)
        if cell.key in _OPENS and cell.state in _OPEN
        else cell
        for cell in cells
    )


def _action(
    snapshot: SourceSnapshot, cells: tuple[StageCell, ...]
) -> NextAction | None:
    clips = snapshot.clips
    if clips[ClipStatus.CHECKED]:
        return NextAction("review", "Open review")
    if clips[ClipStatus.APPROVED]:
        return NextAction("export", "Export")
    if clips[ClipStatus.NEEDS_FIX]:
        return NextAction("fix", "Review flagged clips")
    if snapshot.media != "Imported":
        return None  # Resume cannot import a source
    open_cells = [cell for cell in cells if cell.actionable]
    if any(cell.state is CellState.PENDING for cell in open_cells):
        return NextAction("resume", "Resume")
    if any(cell.state is CellState.FAILED for cell in open_cells):
        return NextAction("resume", "Retry")
    return None


def _working_set(
    settings: Settings, snapshot: SourceSnapshot, statuses: dict[int, ClipStatus]
) -> _Batch:
    """The clips a default batch works on: the first selected ids of the ranking.

    The board cannot know a non-default target after a restart, so it assumes
    the default. A source without a ranking has no clips in practice; if it
    does, all of its clips form the set. An unreadable ranking marks nothing done.
    """
    root = source_dir(settings, snapshot.source_id)
    path = root / "rank.json"
    inside = root.resolve().is_relative_to(settings.data_dir.resolve())
    if not inside or not path.is_file() or path.is_symlink():
        return _Batch(snapshot.clips, sum(snapshot.clips.values()))
    try:
        ids = load_rank(root).selected_ids[:DEFAULT_TARGET]
    except (OSError, ValueError):
        return _Batch(Counter(), 0, known=False)
    found = [statuses[clip_id] for clip_id in ids if clip_id in statuses]
    return _Batch(Counter(found), len(ids))


def _clip_statuses(
    settings: Settings, source_id: int | None
) -> dict[int, dict[int, ClipStatus]]:
    with connect(settings.db_path, read_only=True) as db:
        rows = db.execute(
            "SELECT source_id, id, status FROM clips WHERE (? IS NULL OR source_id = ?)",
            (source_id, source_id),
        ).fetchall()
    found: dict[int, dict[int, ClipStatus]] = {}
    for row in rows:
        found.setdefault(int(row[0]), {})[int(row[1])] = ClipStatus(row[2])
    return found


def _row(
    snapshot: SourceSnapshot, batch: _Batch, running: tuple[int, str] | None
) -> BoardRow:
    reason = permission_reason(snapshot.allowed, snapshot.basis)
    if reason:
        return BoardRow(snapshot, _blocked_cells(reason), None)
    live = running[1] if running and running[0] == snapshot.source_id else None
    cells = _cells(snapshot, batch, None if live == "batch" else live)
    if live == "batch":
        cells = _batch_running(cells)
    cells = _with_check_failures(snapshot, cells)
    return BoardRow(snapshot, cells, _action(snapshot, cells))


def build_rows(
    settings: Settings,
    *,
    running: tuple[int, str] | None,
    source_id: int | None = None,
) -> list[BoardRow]:
    snapshots = _load(settings, source_id)
    statuses = _clip_statuses(settings, source_id)
    return [
        _row(
            snapshot,
            _working_set(settings, snapshot, statuses.get(snapshot.source_id, {})),
            running,
        )
        for snapshot in snapshots
    ]
