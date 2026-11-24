"""Board data for the production board: figures, source cards and outputs.

Plain functions and frozen dataclasses with no HTML, so a page only renders them.
"""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from montaj.board import BoardRow, CellState, NextAction, StageCell
from montaj.config import Settings
from montaj.dashboard_parts import permission_reason
from montaj.db import connect
from montaj.models import CheckResult, Clip, ClipStatus

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

# Stages that spend API money: the values of `stage_jobs.PAID_STAGES`. Candidates
# and Check run locally.
PAID_STAGE_KEYS = frozenset({"transcribe", "rank", "draft"})

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
class Chip:
    """A source's state in one or two words. `tone` is a CSS tone name."""

    label: str
    tone: str


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
    chip: Chip
    current: int


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


def current_stage(segments: tuple[StageSegment, ...]) -> int:
    """The stage a card talks about: the first running one, else the first not done."""
    for index, segment in enumerate(segments):
        if segment.state is CellState.RUNNING:
            return index
    for index, segment in enumerate(segments):
        if segment.state is not CellState.DONE:
            return index
    return len(segments) - 1


def segment_word(segment: StageSegment, is_current: bool) -> str:
    """The word that names a segment's state: never colour alone."""
    if segment.state is CellState.PENDING:
        return "next" if is_current else "waiting"
    return segment.state.value


def chip_for(row: BoardRow, segments: tuple[StageSegment, ...]) -> Chip:
    """The first rule that matches: blocked, running, failed, review, export, confirm."""
    snapshot, action = row.snapshot, row.action
    states = {segment.state for segment in segments}
    if permission_reason(snapshot.allowed, snapshot.basis):
        return Chip("Blocked", "warning")
    if CellState.RUNNING in states:
        return Chip("Running", "running")
    if CellState.FAILED in states or (action and action.kind == "fix"):
        return Chip("Failed", "warning")
    if snapshot.clips[ClipStatus.CHECKED]:
        return Chip("Ready for review", "review")
    if snapshot.clips[ClipStatus.APPROVED]:
        return Chip("Ready to export", "review")
    if action and action.kind == "resume":
        return Chip("Needs your confirm", "review")
    judged = [s for s in segments if _cell_key(s.key) is not None]
    if all(segment.state is CellState.DONE for segment in judged):
        return Chip("Done", "success")
    return Chip("Waiting", "neutral")


def _card(row: BoardRow, source_ceiling: float) -> SourceCard:
    snapshot = row.snapshot
    segments = stage_segments(row.cells)
    return SourceCard(
        snapshot.source_id,
        snapshot.title,
        snapshot.niche,
        snapshot.state,
        snapshot.tone,
        segments,
        row.action,
        snapshot.cost,
        source_ceiling,
        max(source_ceiling - snapshot.cost, 0.0),
        snapshot.clips[ClipStatus.CHECKED] if awaiting_review(row) else 0,
        chip_for(row, segments),
        current_stage(segments),
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


# Clips that have media to show. Candidate, ranked and rejected clips have none.
_RENDERED = (
    ClipStatus.RENDERED,
    ClipStatus.CAPTIONED,
    ClipStatus.DRAFTED,
    ClipStatus.CHECKED,
    ClipStatus.NEEDS_FIX,
    ClipStatus.APPROVED,
    ClipStatus.EXPORTED,
    ClipStatus.POSTED,
)
_REASON_MAX = 160


def _short(text: str | None) -> str:
    text = (text or "").strip()
    return text if len(text) <= _REASON_MAX else text[: _REASON_MAX - 3] + "..."


def _modified(settings: Settings, relative: str | None) -> datetime | None:
    """When a clip's media file last changed, or None when it cannot be trusted."""
    if not relative:
        return None
    path = settings.data_dir / relative
    inside = path.resolve().is_relative_to(settings.data_dir.resolve())
    if not inside or path.is_symlink() or not path.is_file():
        return None
    try:
        return datetime.fromtimestamp(path.stat().st_mtime)
    except OSError:
        return None


def _output(settings: Settings, clip: Clip) -> OutputRecord:
    assert clip.id is not None
    media = clip.final_path or clip.raw_path
    failed = (
        clip.status is ClipStatus.NEEDS_FIX or clip.check_result is CheckResult.FAIL
    )
    return OutputRecord(
        clip.id,
        clip.source_id,
        (clip.hook_text or clip.hook_a or "").strip() or f"Clip {clip.id}",
        Path(media or "").name,
        clip.end_s - clip.start_s,
        clip.status,
        failed,
        _short(clip.decision_reason) if failed else "",
        _modified(settings, media),
    )


def recent_outputs(settings: Settings, limit: int = 5) -> tuple[OutputRecord, ...]:
    """The newest rendered clips across sources. The renderer escapes the text."""
    marks = ", ".join("?" for _ in _RENDERED)
    with connect(settings.db_path, read_only=True) as db:
        rows = db.execute(
            f"SELECT * FROM clips WHERE status IN ({marks}) ORDER BY id DESC LIMIT ?",
            (*(status.value for status in _RENDERED), limit),
        ).fetchall()
    return tuple(_output(settings, Clip.model_validate(dict(row))) for row in rows)
