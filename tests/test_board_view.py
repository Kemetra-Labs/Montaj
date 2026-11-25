from collections import Counter
from typing import Any

import pytest

from montaj.board import STRIP, BoardRow, CellState, NextAction, StageCell
from montaj.board_view import (
    PAID_STAGE_KEYS,
    STAGES,
    awaiting_review,
    build_view,
    chip_for,
    current_stage,
    needs_attention,
    segment_word,
    stage_segments,
)
from montaj.dashboard import SourceSnapshot
from montaj.models import ClipStatus
from montaj.stage_jobs import PAID_STAGES


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


def _states(
    *states: CellState, action: NextAction | None = None, **over: Any
) -> BoardRow:
    return BoardRow(_snapshot(**over), _cells(*states), action)


_P, _D, _R, _F, _B = (
    CellState.PENDING,
    CellState.DONE,
    CellState.RUNNING,
    CellState.FAILED,
    CellState.BLOCKED,
)
_CHECKED = Counter({ClipStatus.CHECKED: 1})
_APPROVED = Counter({ClipStatus.APPROVED: 1})
_RESUME = NextAction("resume", "Resume")


@pytest.mark.parametrize(
    ("row", "chip"),
    [
        (
            _states(_P, _P, _P, _P, _P, _P, _P, allowed=False, clips=_CHECKED),
            ("Blocked", "warning"),
        ),
        (_states(_D, _D, _R, _P, _P, _P, _P, allowed=False), ("Blocked", "warning")),
        (_states(_D, _D, _R, _P, _P, _P, _P), ("Running", "running")),
        (_states(_D, _F, _R, _P, _P, _P, _P), ("Running", "running")),
        (_states(_D, _F, _P, _P, _P, _P, _P), ("Failed", "warning")),
        (
            _states(
                _D,
                _D,
                _D,
                _D,
                _P,
                _P,
                _P,
                action=NextAction("fix", "Review flagged clips"),
            ),
            ("Failed", "warning"),
        ),
        (
            _states(_D, _D, _D, _D, _D, _P, _P, clips=_CHECKED),
            ("Ready for review", "review"),
        ),
        (
            _states(_D, _D, _D, _D, _D, _D, _P, clips=_APPROVED),
            ("Ready to export", "review"),
        ),
        (
            _states(_D, _D, _P, _P, _P, _P, _P, action=_RESUME),
            ("Needs your confirm", "review"),
        ),
        (_states(_D, _D, _D, _D, _D, _D, _D), ("Done", "success")),
        (_states(_P, _P, _P, _P, _P, _P, _P), ("Waiting", "neutral")),
    ],
)
def test_chip_for_each_state(row: BoardRow, chip: tuple[str, str]) -> None:
    result = chip_for(row, stage_segments(row.cells))
    assert (result.label, result.tone) == chip


def test_current_stage_and_segment_words() -> None:
    running = stage_segments(_cells(_D, _D, _R, _P, _B, _B, _B))
    assert current_stage(running) == 2  # first running segment: Candidates
    idle = stage_segments(_cells(_D, _D, _P, _P, _P, _P, _P))
    assert current_stage(idle) == 2  # first segment not done
    finished = stage_segments(_cells(_D, _D, _D, _D, _D, _D, _D))
    assert current_stage(finished) == 10  # Post log is waiting until MJ-30
    assert segment_word(idle[2], True) == "next"
    assert segment_word(idle[3], False) == "waiting"
    assert [segment_word(s, False) for s in running[:2] + running[6:7]] == [
        "done",
        "done",
        "blocked",
    ]
    assert segment_word(running[2], True) == "running"
    failed = stage_segments(_cells(_D, _F, _P, _P, _P, _P, _P))
    assert segment_word(failed[1], True) == "failed"


def test_paid_stage_keys_match_the_board_stages() -> None:
    assert PAID_STAGE_KEYS == {stage.value for stage in PAID_STAGES}


def test_blocked_card_shows_no_review_count() -> None:
    blocked = _states(
        _P,
        _P,
        _P,
        _P,
        _P,
        _P,
        _P,
        allowed=False,
        clips=Counter({ClipStatus.CHECKED: 2}),
    )
    allowed = _states(
        _P, _P, _P, _P, _P, _P, _P, clips=Counter({ClipStatus.CHECKED: 2})
    )
    cards = build_view([blocked, allowed], source_ceiling=1.0, run_ceiling=1.0).cards
    assert (cards[0].clips_to_review, cards[1].clips_to_review) == (0, 2)


def _card_for(row: BoardRow, **kwargs: Any) -> Any:
    return build_view([row], source_ceiling=2.0, run_ceiling=0.5, **kwargs).cards[0]


def test_lock_and_budget_chips() -> None:
    from montaj.board_view import LockInfo

    lock = {1: LockInfo("pipeline", None)}
    plain = _states(_D, _D, _P, _P, _P, _P, _P, action=_RESUME)
    locked = _card_for(plain, locks=lock)
    assert locked.lock == LockInfo("pipeline", None)
    assert (locked.chip.label, locked.chip.tone) == ("Locked", "warning")

    spent = _card_for(_states(_D, _D, _P, _P, _P, _P, _P, action=_RESUME, cost=2.0))
    assert spent.over_budget and spent.chip.label == "Over budget"
    assert not _card_for(plain).over_budget
    assert _card_for(plain).lock is None

    both = _card_for(
        _states(_D, _D, _P, _P, _P, _P, _P, action=_RESUME, cost=3.0), locks=lock
    )
    assert both.chip.label == "Locked"

    blocked = _states(_D, _D, _P, _P, _P, _P, _P, allowed=False, cost=3.0)
    assert _card_for(blocked, locks=lock).chip.label == "Blocked"

    running = _states(_D, _D, _R, _P, _P, _P, _P, cost=3.0)
    assert _card_for(running, locks=lock).chip.label == "Running"


def test_zero_ceiling_is_not_over_budget() -> None:
    row = _states(_D, _D, _P, _P, _P, _P, _P, cost=0.0)
    card = build_view([row], source_ceiling=0.0, run_ceiling=0.5).cards[0]
    assert not card.over_budget
