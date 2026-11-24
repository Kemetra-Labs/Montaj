"""Board data for the production board: figures, source cards and outputs.

Plain functions and frozen dataclasses with no HTML, so a page only renders them.
"""

from dataclasses import dataclass
from datetime import datetime

from montaj.board import BoardRow, CellState, NextAction, StageCell
from montaj.dashboard_parts import permission_reason
from montaj.models import ClipStatus

STAGES: tuple[tuple[str, str], ...] = (
    ("ingest", "Ingest"),
    ("transcribe", "Transcribe"),
    ("candidates", "Candidates"),
    ("rank", "Rank"),
    ("cut", "Cut"),
    ("captions", "Captions"),
    ("draft", "Draft"),
    ("check", "Check"),
    ("review", "Review"),
    ("export", "Export"),
    ("post_log", "Post log"),
)

# The board judges seven groups of these stages (`board.STRIP`). Both stages of a
# group show the group's state until each stage has its own state of record.
_CELL_OF = {
    "candidates": "rank",
    "rank": "rank",
    "cut": "render",
    "captions": "render",
    "draft": "draft",
    "check": "draft",
}


@dataclass(frozen=True, slots=True)
class StageSegment:
    key: str
    label: str
    state: CellState
    reason: str


@dataclass(frozen=True, slots=True)
class SourceCard:
    source_id: int
    title: str
    niche: str
    state: str
    tone: str
    segments: tuple[StageSegment, ...]
    action: NextAction | None
    cost: float
    ceiling: float
    left: float
    clips_to_review: int


@dataclass(frozen=True, slots=True)
class OutputRecord:
    clip_id: int
    source_id: int
    title: str
    file: str
    duration_s: float
    status: ClipStatus
    check_failed: bool
    check_reason: str
    finished_at: datetime | None


@dataclass(frozen=True, slots=True)
class Summary:
    spend: float
    source_count: int
    run_spend: float | None
    run_ceiling: float
    attention: int
    review_clips: int


@dataclass(frozen=True, slots=True)
class BoardView:
    summary: Summary
    cards: tuple[SourceCard, ...]
    outputs: tuple[OutputRecord, ...]


def needs_attention(row: BoardRow) -> bool:
    return row.snapshot.tone == "warning"


def awaiting_review(row: BoardRow) -> bool:
    """A permitted source with at least one checked clip."""
    snapshot = row.snapshot
    return (
        not permission_reason(snapshot.allowed, snapshot.basis)
        and snapshot.clips[ClipStatus.CHECKED] > 0
    )


def _cell_key(stage_key: str) -> str | None:
    if stage_key == "post_log":
        return None  # no board cell judges it yet
    return _CELL_OF.get(stage_key, stage_key)


def stage_segments(cells: tuple[StageCell, ...]) -> tuple[StageSegment, ...]:
    by_key = {cell.key: cell for cell in cells}
    segments = []
    for key, label in STAGES:
        cell = by_key.get(_cell_key(key) or "")
        state, reason = (cell.state, cell.reason) if cell else (CellState.PENDING, "")
        segments.append(StageSegment(key, label, state, reason))
    return tuple(segments)


def _card(row: BoardRow, source_ceiling: float) -> SourceCard:
    snapshot = row.snapshot
    return SourceCard(
        snapshot.source_id,
        snapshot.title,
        snapshot.niche,
        snapshot.state,
        snapshot.tone,
        stage_segments(row.cells),
        row.action,
        snapshot.cost,
        source_ceiling,
        max(source_ceiling - snapshot.cost, 0.0),
        snapshot.clips[ClipStatus.CHECKED],
    )


def build_view(
    rows: list[BoardRow],
    *,
    source_ceiling: float,
    run_ceiling: float,
    run_spend: tuple[float, float] | None = None,
    outputs: tuple[OutputRecord, ...] = (),
) -> BoardView:
    """The board's data. A running job's own ceiling replaces `run_ceiling`.

    A job started with a budget override has a ceiling of its own, as today.
    """
    spent, ceiling = run_spend if run_spend else (None, run_ceiling)
    summary = Summary(
        spend=sum(row.snapshot.cost for row in rows),
        source_count=len(rows),
        run_spend=spent,
        run_ceiling=ceiling,
        attention=sum(needs_attention(row) for row in rows),
        review_clips=sum(
            row.snapshot.clips[ClipStatus.CHECKED]
            for row in rows
            if awaiting_review(row)
        ),
    )
    cards = tuple(_card(row, source_ceiling) for row in rows)
    return BoardView(summary, cards, outputs)
