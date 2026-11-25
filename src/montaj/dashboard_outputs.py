"""Recent outputs: the newest rendered clips and one link into the clip preview.

Review decisions stay in the clip preview, so a row has no approve or reject control.
"""

from datetime import datetime
from html import escape

from montaj.board_view import OUTPUT_SIZE, Chip, OutputRecord
from montaj.dashboard_parts import badge, link_button
from montaj.models import ClipStatus

_NOTE = "Approve and reject happen in the clip preview."
_EMPTY = (
    '<div class="empty-copy"><strong>No clips yet</strong>'
    "<p>Clips appear here once a source is cut and captioned.</p></div>"
)
_CHIPS = {
    ClipStatus.CHECKED: Chip("Ready for review", "review"),
    ClipStatus.APPROVED: Chip("Approved", "success"),
    ClipStatus.EXPORTED: Chip("Exported", "success"),
    ClipStatus.POSTED: Chip("Exported", "success"),
}
_NOT_CHECKED = Chip("Not checked yet", "neutral")


def duration_text(seconds: float) -> str:
    minutes, rest = divmod(int(round(seconds)), 60)
    return f"{minutes}m {rest:02d}s" if minutes else f"{rest}s"


def age_text(finished: datetime | None, now: datetime) -> str:
    """How long ago the media was written. Empty when unknown."""
    if finished is None:
        return ""
    seconds = (now - finished).total_seconds()
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h ago"
    return f"{int(seconds // 86400)} d ago"


def output_chip(record: OutputRecord) -> Chip:
    """Blocked first, then a failed check, then the clip's status."""
    if record.blocked_reason:
        return Chip("Blocked", "warning")
    if record.check_failed:
        return Chip("Check failed", "warning")
    return _CHIPS.get(record.status, _NOT_CHECKED)


def _note_html(record: OutputRecord) -> str:
    if record.blocked_reason:
        return f'<p class="output-note">{escape(record.blocked_reason)}</p>'
    if record.check_failed and record.check_note:
        return f'<p class="output-note warning">{escape(record.check_note)}</p>'
    return ""


def _link_html(record: OutputRecord) -> str:
    """No link for a source that is not permitted; else into the clip preview."""
    if record.blocked_reason:
        return ""
    label = "See check" if record.check_failed else "Open review"
    return link_button(f"/?source={record.source_id}#clip-{record.clip_id}", label)


def _row_html(record: OutputRecord, now: datetime) -> str:
    chip = output_chip(record)
    meta = " · ".join(
        escape(part)
        for part in (record.file, OUTPUT_SIZE, age_text(record.finished_at, now))
        if part
    )
    return (
        '<li class="output"><div class="thumb" aria-hidden="true">'
        f"{duration_text(record.duration_s)}</div>"
        f'<div class="output-body"><h3>{escape(record.title)}</h3>'
        f'<p class="secondary">{meta}</p>{_note_html(record)}'
        f'<div class="output-foot">{badge(chip.label, chip.tone)}'
        f"{_link_html(record)}</div></div></li>"
    )


def outputs_html(outputs: tuple[OutputRecord, ...], now: datetime) -> str:
    """The panel body: a note on where review happens, then the rows or the empty state."""
    note = f'<p class="panel-note">{_NOTE}</p>'
    if not outputs:
        return note + _EMPTY
    rows = "".join(_row_html(record, now) for record in outputs)
    return f'{note}<ol class="outputs" aria-label="Recent outputs">{rows}</ol>'
