"""Board data for the production board: figures, source cards and outputs.

Plain functions and frozen dataclasses with no HTML, so a page only renders them.
"""

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from montaj.board import BoardRow, CellState, NextAction, StageCell
from montaj.config import Settings
from montaj.dashboard_parts import NOT_PERMITTED_REASON, permission_reason
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
    check_note: str
    finished_at: datetime | None
    blocked_reason: str
    # The clip preview lists only clips with a final render that are not posted.
    in_preview: bool


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
# Every render is vertical 1080x1920 (`engine.CUT_STYLE` and `CUT_STYLE_V2`).
OUTPUT_SIZE = "1080×1920"

# Fixed wording for each compliance rule, by the start of the reason `check.py`
# stores. Stored text is never shown: the column also holds ranking reasons.
_CHECK_NOTES = (
    ("Length ", "Length is outside the allowed range."),
    ("Missing required hashtag", "A required hashtag is missing."),
    ("Missing disclosure", "The disclosure is missing."),
    ("Matches banned word or topic", "Uses a banned word or topic."),
    ("Source is not permitted.", "The source is not permitted."),
    ("This clip was already posted.", "This clip was already posted."),
    ("No matching account", "No account matches the daily post cap."),
    ("Possible medical claim", "Possible medical claim or promise."),
    ("Daily post cap is ", "The daily post cap is reached."),
)
_UNKNOWN_CHECK = "Open the clip preview to see why."


def check_note(reason: str | None) -> str:
    """Fixed sentences for the failed rules in a stored reason, once each.

    `check.py` joins rule reasons with "; ", and a reason can hold "; " itself
    (the daily cap, a banned term), so known rule starts are found in place and
    the text up to the next one belongs to them. Text before the first known
    start, or with none, is unknown and gets one fixed sentence.
    """
    text = (reason or "").strip()
    if not text:
        return ""
    starts = sorted(
        (match.start(1), note)
        for start, note in _CHECK_NOTES
        for match in re.finditer(rf"(?:^|; )({re.escape(start)})", text)
    )
    notes: dict[str, None] = {}
    if not starts or starts[0][0] > 0:
        notes[_UNKNOWN_CHECK] = None
    for _, note in starts:
        notes[note] = None
    return " ".join(notes)


def _modified(settings: Settings, relative: str | None) -> datetime | None:
    """When a clip's media file last changed, or None when it cannot be trusted.

    An odd stored path (outside the data directory, a symlink, a NUL byte, a
    link loop) gives None, never an error: the board loads this on every visit.
    """
    if not relative:
        return None
    path = settings.data_dir / relative
    try:
        inside = path.resolve().is_relative_to(settings.data_dir.resolve())
        if not inside or path.is_symlink() or not path.is_file():
            return None
        return datetime.fromtimestamp(path.stat().st_mtime)
    except (OSError, ValueError, RuntimeError):
        return None


def _title(clip: Clip) -> str:
    for text in (clip.hook_text, clip.hook_a):
        if text and text.strip():
            return text.strip()
    return f"Clip {clip.id}"


def _output(settings: Settings, clip: Clip, blocked: str) -> OutputRecord:
    assert clip.id is not None
    media = clip.final_path or clip.raw_path
    failed = (
        clip.status is ClipStatus.NEEDS_FIX or clip.check_result is CheckResult.FAIL
    )
    return OutputRecord(
        clip.id,
        clip.source_id,
        _title(clip),
        Path(media or "").name,
        clip.end_s - clip.start_s,
        clip.status,
        failed,
        check_note(clip.decision_reason) if failed else "",
        _modified(settings, media),
        blocked,
        clip.final_path is not None and clip.status is not ClipStatus.POSTED,
    )


def recent_outputs(settings: Settings, limit: int = 5) -> tuple[OutputRecord, ...]:
    """The newest rendered clips across sources. The renderer escapes the text."""
    if limit < 1:
        raise ValueError("Recent outputs need a limit of at least 1.")
    marks = ", ".join("?" for _ in _RENDERED)
    with connect(settings.db_path, read_only=True) as db:
        rows = db.execute(
            f"SELECT * FROM clips WHERE status IN ({marks}) ORDER BY id DESC LIMIT ?",
            (*(status.value for status in _RENDERED), limit),
        ).fetchall()
        clips = [Clip.model_validate(dict(row)) for row in rows]
        ids = sorted({clip.source_id for clip in clips})
        found = db.execute(
            "SELECT id, source_allowed, allowed_basis FROM sources "
            f"WHERE id IN ({', '.join('?' for _ in ids)})",
            ids,
        ).fetchall()
    reasons = {int(row[0]): permission_reason(bool(row[1]), row[2]) for row in found}
    return tuple(
        _output(settings, clip, reasons.get(clip.source_id, NOT_PERMITTED_REASON))
        for clip in clips
    )
