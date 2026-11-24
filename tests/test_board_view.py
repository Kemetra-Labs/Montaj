from collections import Counter
from typing import Any

import pytest

from montaj.board import STRIP, BoardRow, CellState, NextAction, StageCell
from montaj.board_view import (
    STAGES,
    awaiting_review,
    build_view,
    needs_attention,
    stage_segments,
)
from montaj.dashboard import SourceSnapshot
from montaj.models import ClipStatus


def _snapshot(**over: Any) -> SourceSnapshot:
    fields: dict[str, Any] = {
        "source_id": 1,
        "title": "A",
        "niche": "Health",
        "added": "2026-11-24",
        "allowed": True,
        "basis": "Creator approval",
        "state": "Imported",
        "tone": "success",
        "problem": None,
        "media": "Imported",
        "duration": "1m 00s",
        "checkpoints": (),
        "clips": Counter(),
        "cost": 0.0,
    }
    fields.update(over)
    return SourceSnapshot(**fields)


def _cells(*states: CellState) -> tuple[StageCell, ...]:
    return tuple(
        StageCell(key, label, state) for (key, label), state in zip(STRIP, states)
    )


def _row(action: NextAction | None = None, **over: Any) -> BoardRow:
    return BoardRow(
        _snapshot(**over), _cells(*[CellState.PENDING] * len(STRIP)), action
    )


def test_stages_are_the_eleven_in_order() -> None:
    assert [key for key, _ in STAGES] == [
        "ingest",
        "transcribe",
        "candidates",
        "rank",
        "cut",
        "captions",
        "draft",
        "check",
        "review",
        "export",
        "post_log",
    ]
    assert STAGES[-1] == ("post_log", "Post log")


def test_group_state_is_shared_by_both_stages() -> None:
    cells = list(
        _cells(
            CellState.DONE,
            CellState.DONE,
            CellState.RUNNING,
            CellState.PENDING,
            CellState.BLOCKED,
            CellState.BLOCKED,
            CellState.BLOCKED,
        )
    )
    cells[4] = StageCell(
        "draft", "Draft + check", CellState.BLOCKED, "Cut + captions first"
    )
    segments = stage_segments(tuple(cells))
    assert [segment.state for segment in segments] == [
        CellState.DONE,
        CellState.DONE,
        CellState.RUNNING,
        CellState.RUNNING,
        CellState.PENDING,
        CellState.PENDING,
        CellState.BLOCKED,
        CellState.BLOCKED,
        CellState.BLOCKED,
        CellState.BLOCKED,
        CellState.PENDING,
    ]
    assert [segment.key for segment in segments] == [key for key, _ in STAGES]
    assert [segment.reason for segment in segments[6:8]] == ["Cut + captions first"] * 2
    assert segments[-1].reason == ""


def test_awaiting_review_ignores_unpermitted_source() -> None:
    checked = Counter({ClipStatus.CHECKED: 1})
    assert awaiting_review(_row(allowed=False, clips=checked)) is False
    assert awaiting_review(_row(allowed=True, clips=checked)) is True
    assert awaiting_review(_row(allowed=True, clips=Counter())) is False


def test_needs_attention_follows_the_warning_tone() -> None:
    assert needs_attention(_row(tone="warning")) is True
    assert needs_attention(_row(tone="review")) is False


def _three_rows() -> list[BoardRow]:
    return [
        _row(source_id=1, cost=0.5, tone="warning"),
        _row(
            source_id=2,
            cost=0.25,
            tone="review",
            clips=Counter({ClipStatus.CHECKED: 2}),
        ),
        _row(
            source_id=3,
            cost=0.0,
            tone="neutral",
            allowed=False,
            clips=Counter({ClipStatus.CHECKED: 1}),
        ),
    ]


def test_summary_counts_match_the_old_board() -> None:
    summary = build_view(_three_rows(), source_ceiling=5.0, run_ceiling=5.0).summary
    assert summary.spend == pytest.approx(0.75)
    assert summary.source_count == 3
    assert summary.attention == 1
    assert summary.review_clips == 2
    assert summary.run_spend is None
    assert summary.run_ceiling == 5.0


def test_running_job_ceiling_wins() -> None:
    summary = build_view(
        _three_rows(), source_ceiling=5.0, run_ceiling=5.0, run_spend=(0.5, 2.0)
    ).summary
    assert summary.run_spend == 0.5
    assert summary.run_ceiling == 2.0


def test_card_dollars_left_never_negative() -> None:
    under = build_view([_row(cost=0.4)], source_ceiling=1.0, run_ceiling=5.0)
    over = build_view([_row(cost=1.5)], source_ceiling=1.0, run_ceiling=5.0)
    assert under.cards[0].left == pytest.approx(0.6)
    assert over.cards[0].left == 0.0
    assert over.cards[0].ceiling == 1.0


def test_card_carries_the_row() -> None:
    action = NextAction("resume", "Resume")
    row = _row(action, source_id=4, title="Interview", state="Imported", tone="success")
    card = build_view([row], source_ceiling=1.0, run_ceiling=5.0).cards[0]
    assert (card.source_id, card.title, card.state, card.tone) == (
        4,
        "Interview",
        "Imported",
        "success",
    )
    assert card.action == action
    assert len(card.segments) == len(STAGES)


def test_empty_board() -> None:
    view = build_view([], source_ceiling=5.0, run_ceiling=5.0)
    assert view.cards == () and view.outputs == ()
    summary = view.summary
    assert (summary.spend, summary.source_count) == (0, 0)
    assert (summary.attention, summary.review_clips) == (0, 0)
