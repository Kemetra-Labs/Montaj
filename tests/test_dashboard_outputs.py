from datetime import datetime, timedelta

import pytest

from montaj.board_view import OutputRecord
from montaj.dashboard_outputs import age_text, duration_text, outputs_html
from montaj.models import ClipStatus

_NOW = datetime(2026, 11, 25, 12, 0, 0)
_BLOCKED = "Record permission for this source first."


def _record(**over: object) -> OutputRecord:
    fields: dict[str, object] = {
        "clip_id": 4,
        "source_id": 7,
        "title": "Why the first three seconds matter",
        "file": "final.mp4",
        "duration_s": 42.0,
        "status": ClipStatus.CHECKED,
        "check_failed": False,
        "check_note": "",
        "finished_at": _NOW - timedelta(minutes=3),
        "blocked_reason": "",
        "in_preview": True,
    }
    fields.update(over)
    return OutputRecord(**fields)  # type: ignore[arg-type]


def test_ready_row() -> None:
    html = outputs_html((_record(),), _NOW)
    assert "Why the first three seconds matter" in html
    assert "final.mp4 · 1080×1920 · 3 min ago" in html
    assert '<span class="badge review">Ready for review</span>' in html
    assert 'href="/?source=7#clip-4">Open review</a>' in html
    assert 'class="thumb" aria-hidden="true">42s</div>' in html


def test_failed_row_shows_the_fixed_reason() -> None:
    record = _record(
        status=ClipStatus.NEEDS_FIX,
        check_failed=True,
        check_note="Length is outside the allowed range.",
    )
    html = outputs_html((record,), _NOW)
    assert '<span class="badge warning">Check failed</span>' in html
    assert (
        '<p class="output-note warning">Length is outside the allowed range.</p>'
        in html
    )
    assert 'href="/?source=7#clip-4">See check</a>' in html


def test_blocked_row_has_no_link() -> None:
    html = outputs_html((_record(blocked_reason=_BLOCKED),), _NOW)
    assert '<span class="badge warning">Blocked</span>' in html
    assert _BLOCKED in html
    assert '<a class="button' not in html and 'href="/?source=' not in html


def test_blocked_wins_over_a_failed_check() -> None:
    record = _record(
        blocked_reason=_BLOCKED, check_failed=True, check_note="Uses a banned word."
    )
    html = outputs_html((record,), _NOW)
    assert ">Blocked</span>" in html and "Check failed" not in html


@pytest.mark.parametrize(
    ("status", "chip"),
    [
        (ClipStatus.APPROVED, '<span class="badge success">Approved</span>'),
        (ClipStatus.EXPORTED, '<span class="badge success">Exported</span>'),
        (ClipStatus.POSTED, '<span class="badge success">Exported</span>'),
        (ClipStatus.RENDERED, '<span class="badge neutral">Not checked yet</span>'),
        (ClipStatus.CAPTIONED, '<span class="badge neutral">Not checked yet</span>'),
        (ClipStatus.DRAFTED, '<span class="badge neutral">Not checked yet</span>'),
    ],
)
def test_chip_for_each_status(status: ClipStatus, chip: str) -> None:
    assert chip in outputs_html((_record(status=status),), _NOW)


def test_text_is_escaped() -> None:
    html = outputs_html((_record(title="<b>x</b>", file="<i>f</i>.mp4"),), _NOW)
    assert "&lt;b&gt;x&lt;/b&gt;" in html and "&lt;i&gt;f&lt;/i&gt;.mp4" in html
    assert "<b>x" not in html and "<i>f" not in html


def test_meta_leaves_out_unknown_parts() -> None:
    html = outputs_html((_record(file="", finished_at=None),), _NOW)
    assert '<p class="secondary">1080×1920</p>' in html


@pytest.mark.parametrize(
    ("ago", "text"),
    [
        (timedelta(seconds=20), "just now"),
        (timedelta(minutes=5), "5 min ago"),
        (timedelta(hours=3), "3 h ago"),
        (timedelta(days=2), "2 d ago"),
    ],
)
def test_age_text(ago: timedelta, text: str) -> None:
    assert age_text(_NOW - ago, _NOW) == text


def test_age_and_duration_edges() -> None:
    assert age_text(None, _NOW) == ""
    assert age_text(_NOW + timedelta(minutes=5), _NOW) == "just now"
    assert duration_text(42) == "42s"
    assert duration_text(65) == "1m 05s"


def test_empty_state() -> None:
    html = outputs_html((), _NOW)
    assert "No clips yet" in html
    assert "Clips appear here once a source is cut and captioned." in html


def test_heading_note_and_no_review_controls() -> None:
    html = outputs_html((_record(), _record(clip_id=5)), _NOW)
    assert "Approve and reject happen in the clip preview." in html
    assert "<form" not in html and "<button" not in html
    assert html.count('<li class="output">') == 2


def test_record_order_is_kept() -> None:
    html = outputs_html((_record(clip_id=9), _record(clip_id=3)), _NOW)
    assert html.index("#clip-9") < html.index("#clip-3")


def test_clip_missing_from_the_preview_has_no_link() -> None:
    html = outputs_html((_record(in_preview=False),), _NOW)
    assert 'href="/?source=' not in html and "Ready for review" in html
