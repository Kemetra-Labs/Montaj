"""Stage progress cells and next actions for the production board."""

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Literal

from montaj.config import Settings
from montaj.dashboard import SourceSnapshot, _load
from montaj.dashboard_parts import permission_reason
from montaj.models import ClipStatus


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


@dataclass(frozen=True, slots=True)
class NextAction:
    kind: Literal["resume", "review", "export", "fix"]
    label: str


@dataclass(frozen=True, slots=True)
class BoardRow:
    snapshot: SourceSnapshot
    cells: tuple[StageCell, ...]
    action: NextAction | None


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
_OPEN = {CellState.PENDING, CellState.BLOCKED}
_MEDIA_FAILURES = {"Media missing", "Metadata unreadable", "Media unavailable"}
_FIX_REASON = "Fix the flagged clips, then run this stage again."
# A clip-stage cell is done once at least one clip holds a status in its set.
_CAPTIONED = {
    ClipStatus.CAPTIONED,
    ClipStatus.DRAFTED,
    ClipStatus.CHECKED,
    ClipStatus.NEEDS_FIX,
    ClipStatus.APPROVED,
    ClipStatus.EXPORTED,
    ClipStatus.POSTED,
}
_CHECKED = {
    ClipStatus.CHECKED,
    ClipStatus.APPROVED,
    ClipStatus.EXPORTED,
    ClipStatus.POSTED,
}
_APPROVED = {ClipStatus.APPROVED, ClipStatus.EXPORTED, ClipStatus.POSTED}
_EXPORTED = {ClipStatus.EXPORTED, ClipStatus.POSTED}
_CLIP_DONE = {
    "render": _CAPTIONED,
    "draft": _CHECKED,
    "review": _APPROVED,
    "export": _EXPORTED,
}


def _saved(snapshot: SourceSnapshot, label: str) -> bool:
    return dict(snapshot.checkpoints).get(label) == "Saved"


def _reached(snapshot: SourceSnapshot, statuses: set[ClipStatus]) -> bool:
    return any(snapshot.clips[status] for status in statuses)


def _evidence(snapshot: SourceSnapshot, key: str) -> tuple[CellState, str] | None:
    """Return a done or failed state for a stage, or None if it has not run."""
    if key == "ingest":
        if snapshot.media == "Imported":
            return CellState.DONE, ""
        if snapshot.media in _MEDIA_FAILURES:
            return CellState.FAILED, snapshot.media
    elif key == "transcribe":
        if snapshot.problem:
            return CellState.FAILED, snapshot.problem
        if _saved(snapshot, "Transcript"):
            return CellState.DONE, ""
    elif key == "rank":
        if _saved(snapshot, "Candidates") and _saved(snapshot, "Ranking"):
            return CellState.DONE, ""
    elif key == "draft" and snapshot.clips[ClipStatus.NEEDS_FIX]:
        return CellState.FAILED, _FIX_REASON
    if key in _CLIP_DONE and _reached(snapshot, _CLIP_DONE[key]):
        return CellState.DONE, ""
    return None


def _cells(snapshot: SourceSnapshot, running: str | None) -> tuple[StageCell, ...]:
    cells: list[StageCell] = []
    first_open: str | None = None
    for key, label in STRIP:
        found = _evidence(snapshot, key)
        if key == running:
            cell = StageCell(key, label, CellState.RUNNING)
        elif found is not None:
            cell = StageCell(key, label, found[0], found[1])
        elif first_open is not None:
            cell = StageCell(key, label, CellState.BLOCKED, f"{first_open} first")
        else:
            cell = StageCell(key, label, CellState.PENDING)
        if first_open is None and cell.state is not CellState.DONE:
            first_open = label
        cells.append(cell)
    return tuple(cells)


def _blocked_cells(reason: str) -> tuple[StageCell, ...]:
    return tuple(
        StageCell(key, label, CellState.BLOCKED, reason) for key, label in STRIP
    )


def _with_check_failures(
    snapshot: SourceSnapshot, cells: tuple[StageCell, ...]
) -> tuple[StageCell, ...]:
    """Flagged clips block export, and block review when nothing is left to review."""
    if not snapshot.clips[ClipStatus.NEEDS_FIX]:
        return cells
    reviewable = (
        snapshot.clips[ClipStatus.CHECKED] or snapshot.clips[ClipStatus.APPROVED]
    )
    blocked = {"export"} if reviewable else {"review", "export"}
    return tuple(
        replace(cell, state=CellState.BLOCKED, reason=_CHECK_FAILED)
        if cell.key in blocked and cell.state in _OPEN
        else cell
        for cell in cells
    )


def _action(
    snapshot: SourceSnapshot, cells: tuple[StageCell, ...]
) -> NextAction | None:
    if snapshot.clips[ClipStatus.NEEDS_FIX]:
        return NextAction("fix", "Fix flagged clips")
    if snapshot.clips[ClipStatus.CHECKED]:
        return NextAction("review", "Open review")
    if snapshot.clips[ClipStatus.APPROVED]:
        return NextAction("export", "Export")
    if any(cell.state is CellState.PENDING for cell in cells):
        return NextAction("resume", "Resume")
    if any(cell.state is CellState.FAILED for cell in cells):
        return NextAction("resume", "Retry")
    return None


def _row(snapshot: SourceSnapshot, running: tuple[int, str] | None) -> BoardRow:
    reason = permission_reason(snapshot.allowed, snapshot.basis)
    if reason:
        return BoardRow(snapshot, _blocked_cells(reason), None)
    live = running[1] if running and running[0] == snapshot.source_id else None
    cells = _with_check_failures(snapshot, _cells(snapshot, live))
    return BoardRow(snapshot, cells, _action(snapshot, cells))


def build_rows(
    settings: Settings,
    *,
    running: tuple[int, str] | None,
    source_id: int | None = None,
) -> list[BoardRow]:
    return [_row(snapshot, running) for snapshot in _load(settings, source_id)]
