from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from montaj.board import FIX_GUIDANCE, INGEST_STEP, build_rows
from montaj.board_view import LockInfo, build_view
from montaj.config import Settings
from montaj.costs import Service, Usage, log_api_call
from montaj.dashboard_cards import cards_html, legend_html
from montaj.dashboard_parts import BoardContext
from montaj.db import connect, insert
from montaj.models import ClipStatus, Niche, Source
from tests.dashboard_helpers import _add_clip, _add_source, _imported

_SAVED = ("transcript.json", "candidates.json", "rank.json")
_NOW = datetime(2026, 11, 25, 12, tzinfo=UTC)


def _cards(
    db_path: Path,
    *,
    running: tuple[int, str] | None = None,
    operations: bool = True,
    ceiling: float = 0.5,
    locks: dict[int, LockInfo] | None = None,
) -> str:
    rows = build_rows(Settings(data_dir=db_path.parent), running=running)
    view = build_view(rows, source_ceiling=ceiling, run_ceiling=0.5, locks=locks)
    ctx = BoardContext(
        operations,
        running,
        "board-token",
        source_ceiling=ceiling,
        run_ceiling=0.5,
    )
    ids = [row.snapshot.source_id for row in rows]
    return cards_html(view, rows, ctx, ids, now=_NOW)


def _bar(html: str) -> str:
    return html.split('<div class="stage-bar"', 1)[1].split("</div>", 1)[0]


def _label(html: str) -> str:
    return _bar(html).split('aria-label="', 1)[1].split('"', 1)[0]


def _add_failed(db_path: Path) -> None:
    with connect(db_path) as db:
        insert(
            db,
            Source(
                id=7,
                path="input.mp4",
                title="Interview",
                niche=Niche.HEALTH,
                source_allowed=True,
                allowed_basis="Creator approval",
                speech_status="failed",
            ),
        )


def _spend(db_path: Path, cost: float) -> None:
    with connect(db_path) as db:
        log_api_call(db, 7, "transcribe", Service.DEEPGRAM, Usage(seconds=12), cost)


def test_bar_aria_label_lists_every_stage_and_state(db_path: Path) -> None:
    _add_source(db_path)
    _imported(db_path, *_SAVED)
    html = _cards(db_path)
    assert _label(html) == (
        "Stage progress: Ingest done, Transcribe done, Candidates done, Rank done, "
        "Cut next, Captions waiting, Draft blocked, Check blocked, Review blocked, "
        "Export blocked, Post log waiting"
    )
    assert _bar(html).count('class="seg ') == 11
    assert 'role="img"' in html


def _blocked(db: Path) -> None:
    _add_source(db, allowed=False, basis="")


def _failed(db: Path) -> None:
    _add_failed(db)


def _review(db: Path) -> None:
    _add_source(db)
    _imported(db, *_SAVED)
    _add_clip(db, ClipStatus.CHECKED)


def _export(db: Path) -> None:
    _add_source(db)
    _imported(db, *_SAVED)
    _add_clip(db, ClipStatus.APPROVED)


def _confirm(db: Path) -> None:
    _add_source(db)
    _imported(db, "transcript.json")


def _waiting(db: Path) -> None:
    _add_source(db)


def _done(db: Path) -> None:
    _add_source(db)
    _imported(db, *_SAVED)
    _add_clip(db, ClipStatus.EXPORTED)


@pytest.mark.parametrize(
    ("seed", "chip", "word"),
    [
        (_blocked, '<span class="badge warning">Blocked</span>', "blocked"),
        (_failed, '<span class="badge warning">Failed</span>', "failed"),
        (_review, '<span class="badge review">Ready for review</span>', "done"),
        (_export, '<span class="badge review">Ready to export</span>', "done"),
        (_confirm, '<span class="badge review">Needs your confirm</span>', "next"),
        (_waiting, '<span class="badge neutral">Waiting</span>', "next"),
        (_done, '<span class="badge success">Done</span>', "done"),
    ],
)
def test_chip_and_bar_states(db_path: Path, seed: object, chip: str, word: str) -> None:
    seed(db_path)  # type: ignore[operator]
    html = _cards(db_path)
    assert chip in html
    assert f'<span class="seg {word}">' in _bar(html)


def test_running_chip_and_segment(db_path: Path) -> None:
    _confirm(db_path)
    html = _cards(db_path, running=(7, "batch"))
    assert '<span class="badge running">Running</span>' in html
    assert '<span class="seg running">' in _bar(html)
    assert "Rank running" in _label(html)


@pytest.mark.parametrize(
    ("allowed", "basis", "reason"),
    [
        (False, "", "Record permission for this source first."),
        (True, " ", "Record a permission basis for this source first."),
    ],
)
def test_blocked_source_card_has_no_actions_and_says_why(
    db_path: Path, allowed: bool, basis: str, reason: str
) -> None:
    _add_source(db_path, allowed=allowed, basis=basis)
    _imported(db_path, "transcript.json")
    html = _cards(db_path)
    assert "<button" not in html and "<form" not in html and "/confirm" not in html
    assert html.count(reason) == 1
    assert "<strong>Blocked</strong>" in html


def test_busy_board_disables_paid_actions(db_path: Path) -> None:
    _confirm(db_path)
    html = _cards(db_path, running=(2, "batch"))
    assert "<button disabled" in html and "Another job is running (source 2)" in html
    assert "/confirm" not in html


def test_review_link_survives_a_busy_board(db_path: Path) -> None:
    _review(db_path)
    html = _cards(db_path, running=(2, "batch"))
    assert 'href="/?source=7">Open review</a>' in html


def test_resume_and_retry_open_the_cost_confirmation(db_path: Path) -> None:
    _confirm(db_path)
    assert 'href="/dashboard/sources/7/confirm?stage=batch">Resume</a>' in _cards(
        db_path
    )
    read_only = _cards(db_path, operations=False)
    assert "/confirm" not in read_only and "<button" not in read_only


def test_retry_opens_the_cost_confirmation(db_path: Path) -> None:
    _add_failed(db_path)
    _imported(db_path)
    html = _cards(db_path)
    assert 'href="/dashboard/sources/7/confirm?stage=batch">Retry</a>' in html
    assert "<strong>Retry Transcribe</strong>" in html


@pytest.mark.parametrize(
    ("cost", "text"),
    [
        (0.012345, "Source budget $0.01 of $0.50 · $0.49 left"),
        (0.6, "Source budget $0.60 of $0.50 · $0.00 left"),
    ],
)
def test_budget_line(db_path: Path, cost: float, text: str) -> None:
    _add_source(db_path)
    _spend(db_path, cost)
    assert text in _cards(db_path)


def test_budget_line_escapes_a_tiny_cost(db_path: Path) -> None:
    _add_source(db_path)
    _spend(db_path, 0.0002)
    html = _cards(db_path)
    assert "&lt;$0.001 of $0.50" in html and "<$0.001" not in html


def test_title_is_escaped(db_path: Path) -> None:
    with connect(db_path) as db:
        insert(
            db,
            Source(
                id=7,
                path="x.mp4",
                title="<b>x</b>",
                niche=Niche.HEALTH,
                source_allowed=True,
                allowed_basis="Creator approval",
            ),
        )
    html = _cards(db_path)
    assert "&lt;b&gt;x&lt;/b&gt;" in html and "<b>x" not in html


def test_bar_meta_names_the_stage_and_whether_it_is_paid(db_path: Path) -> None:
    _add_source(db_path)
    assert "Stage 1 of 11 · Ingest · free stage" in _cards(db_path)
    _imported(db_path)
    assert "Stage 2 of 11 · Transcribe · paid stage" in _cards(db_path)


def test_thumbnail_shows_the_duration(db_path: Path) -> None:
    _add_source(db_path)
    assert 'class="thumb" aria-hidden="true"></div>' in _cards(db_path)
    _imported(db_path)
    assert 'class="thumb" aria-hidden="true">1m 12s</div>' in _cards(db_path)


def _clips(db_path: Path, status: ClipStatus, count: int) -> None:
    _add_source(db_path)
    _imported(db_path, *_SAVED)
    for clip_id in range(1, count + 1):
        _add_clip(db_path, status, clip_id=clip_id)


@pytest.mark.parametrize(
    ("status", "count", "bold", "muted"),
    [
        (
            ClipStatus.CHECKED,
            1,
            "1 checked clip ready for review",
            "Nothing is exported until you approve it.",
        ),
        (
            ClipStatus.CHECKED,
            2,
            "2 checked clips ready for review",
            "Nothing is exported until you approve it.",
        ),
        (
            ClipStatus.APPROVED,
            2,
            "2 approved clips ready to export",
            "Review is done for these clips.",
        ),
        (ClipStatus.NEEDS_FIX, 1, "1 flagged clip needs a fix", FIX_GUIDANCE),
        (ClipStatus.NEEDS_FIX, 2, "2 flagged clips need a fix", FIX_GUIDANCE),
    ],
)
def test_next_sentences_for_clips(
    db_path: Path, status: ClipStatus, count: int, bold: str, muted: str
) -> None:
    _clips(db_path, status, count)
    html = _cards(db_path)
    assert f"<strong>{bold}</strong>" in html and muted in html


def test_next_sentences_for_stages(db_path: Path) -> None:
    _confirm(db_path)
    html = _cards(db_path)
    assert "<strong>Resume from Candidates</strong>" in html
    assert (
        "Runs every unfinished stage through Draft. "
        "The estimate is shown before anything is paid." in html
    )


def test_next_sentences_when_there_is_nothing_to_run(db_path: Path) -> None:
    _waiting(db_path)
    html = _cards(db_path)
    assert "<strong>Waiting for import</strong>" in html and INGEST_STEP in html


def test_next_sentence_for_a_finished_source(db_path: Path) -> None:
    _done(db_path)
    assert "<strong>Nothing to run</strong>" in _cards(db_path)


def test_legend() -> None:
    html = legend_html()
    assert "11 stages" in html
    for word in ("Done", "Running", "Next", "Blocked", "Failed", "Waiting"):
        assert word in html
    assert 'aria-label="Stage legend"' in html


def test_guidance_is_not_repeated_in_the_notes(db_path: Path) -> None:
    _clips(db_path, ClipStatus.NEEDS_FIX, 1)
    assert _cards(db_path).count(FIX_GUIDANCE) == 1


def test_ingest_step_is_not_repeated_in_the_notes(db_path: Path) -> None:
    _waiting(db_path)
    assert _cards(db_path).count(INGEST_STEP) == 1


def test_failed_ingest_card_keeps_the_cause_and_does_not_say_waiting(
    db_path: Path,
) -> None:
    with connect(db_path) as db:
        insert(
            db,
            Source(
                id=7,
                path="input.mp4",
                title="Interview",
                niche=Niche.HEALTH,
                source_allowed=True,
                allowed_basis="Creator approval",
                speech_status="failed",
                failure_note="needs a javascript runtime",
            ),
        )
    html = _cards(db_path)
    assert "Install Deno" in html
    assert "<strong>Ingest failed</strong>" in html
    assert "Waiting for import" not in html
    assert html.count(INGEST_STEP) == 1


def test_missing_media_card_keeps_the_cause(db_path: Path) -> None:
    _add_source(db_path)
    _imported(db_path)
    (db_path.parent / "sources/7/video.mp4").unlink()
    html = _cards(db_path)
    assert "Media missing." in html
    assert "<strong>Ingest failed</strong>" in html and "Waiting for import" not in html
    assert html.count(INGEST_STEP) == 1


def test_running_card_says_what_is_running(db_path: Path) -> None:
    _confirm(db_path)
    html = _cards(db_path, running=(7, "batch"))
    assert "<strong>Running Candidates</strong>" in html
    assert "This page refreshes while the job runs." in html
    assert "Nothing to run" not in html


def test_locked_card(db_path: Path) -> None:
    _confirm(db_path)
    lock = LockInfo("pipeline", _NOW - timedelta(minutes=4))
    html = _cards(db_path, locks={7: lock})
    assert ">Locked<" in html
    assert "Locked by pipeline. This lock is 4 minutes old." in html
    assert '<button disabled class="button"' in html and ">Resume</button>" in html
    assert "Locked by another command" in html and "/confirm" not in html


def test_unreadable_lock_card(db_path: Path) -> None:
    _confirm(db_path)
    html = _cards(db_path, locks={7: LockInfo("", None)})
    assert "Locked. The lock age is unknown." in html and "/confirm" not in html


def test_over_budget_card(db_path: Path) -> None:
    _add_failed(db_path)
    _imported(db_path)
    _spend(db_path, 2.0)
    html = _cards(db_path, ceiling=2.0)
    assert ">Over budget<" in html
    assert "Over its $2.00 source ceiling. Paid stages are off for this source." in html
    assert ">Retry</button>" in html and "Over the source ceiling" in html
    assert "/confirm" not in html


def test_over_budget_keeps_review_links(db_path: Path) -> None:
    _add_source(db_path)
    _imported(db_path, *_SAVED)
    _add_clip(db_path, ClipStatus.CHECKED)
    _spend(db_path, 2.0)
    html = _cards(db_path, ceiling=2.0)
    assert 'href="/?source=7">' in html and "<button" not in html


def test_read_only_locked_card_has_no_button(db_path: Path) -> None:
    _confirm(db_path)
    html = _cards(db_path, operations=False, locks={7: LockInfo("rank", None)})
    assert "<button" not in html and "Locked by rank." in html
