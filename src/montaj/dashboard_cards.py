"""Active runs: one card per source with a state chip, a stage bar and one next action."""

from html import escape

from montaj.board import FIX_GUIDANCE, INGEST_STEP, BoardRow, StageCell
from montaj.board_view import (
    PAID_STAGE_KEYS,
    STAGES,
    BoardView,
    SourceCard,
    segment_word,
)
from montaj.dashboard_parts import (
    BASE_PATH,
    PERMISSION_REASONS,
    BoardContext,
    badge,
    busy_html,
    format_usd,
    link_button,
    permission_reason,
)
from montaj.models import ClipStatus

_LEGEND = ("Done", "Running", "Next", "Blocked", "Failed", "Waiting")
_RESUME_NOTE = (
    "Runs every unfinished stage through Draft. "
    "The estimate is shown before anything is paid."
)


def _money(amount: float) -> str:
    """Dollar text safe for HTML: `format_usd` can return `<$0.001`."""
    return escape(format_usd(amount))


def _note(cell: StageCell) -> str:
    """A cell reason for the card notes. A pending note names its stage."""
    if cell.state.value == "pending":
        return f"{cell.label}: {cell.reason}"
    return cell.reason


def _notes_html(cells: tuple[StageCell, ...], said: str = "") -> str:
    """Each distinct failure, blocked or progress note once, in strip order.

    A permission reason is not repeated here, and neither is a note that holds
    the sentence the next-action line already says (`said`).
    """
    notes = (
        _note(cell)
        for cell in cells
        if cell.reason and cell.reason not in PERMISSION_REASONS
    )
    reasons = dict.fromkeys(note for note in notes if not (said and said in note))
    if not reasons:
        return ""
    items = "".join(f"<li>{escape(reason)}</li>" for reason in reasons)
    return f'<ul class="cell-notes">{items}</ul>'


def _resume_html(source_id: int, label: str, ctx: BoardContext) -> str:
    """Resume or Retry spends API money, so it opens the cost confirmation."""
    if ctx.running is not None:
        return busy_html(label, ctx.running, source_id=source_id)
    return link_button(f"{BASE_PATH}/sources/{source_id}/confirm?stage=batch", label)


def _action_html(row: BoardRow, ctx: BoardContext) -> str:
    action, source_id = row.action, row.snapshot.source_id
    if action is None:
        return ""
    if action.kind == "resume":
        return _resume_html(source_id, action.label, ctx) if ctx.operations else ""
    target = {
        "review": f"/?source={source_id}",
        "export": f"/?source={source_id}&amp;ready=1",
        "fix": f"/?source={source_id}",
    }[action.kind]
    return link_button(target, action.label)


def _count(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def _sentence(card: SourceCard, row: BoardRow) -> tuple[str, str]:
    """The bold line naming the next step and the muted line under it."""
    snapshot, action = row.snapshot, row.action
    reason = permission_reason(snapshot.allowed, snapshot.basis)
    if reason:
        return "Blocked", reason
    if action is None:
        if snapshot.media != "Imported":
            return "Waiting for import", INGEST_STEP
        return "Nothing to run", ""
    stage = card.segments[card.current].label
    clips = snapshot.clips
    if action.kind == "resume":
        verb = "Retry" if action.label == "Retry" else "Resume from"
        return f"{verb} {stage}", _RESUME_NOTE
    if action.kind == "review":
        count = _count(clips[ClipStatus.CHECKED], "checked clip")
        return f"{count} ready for review", "Nothing is exported until you approve it."
    if action.kind == "export":
        count = _count(clips[ClipStatus.APPROVED], "approved clip")
        return f"{count} ready to export", "Review is done for these clips."
    flagged = clips[ClipStatus.NEEDS_FIX]
    verb = "needs" if flagged == 1 else "need"
    return f"{_count(flagged, 'flagged clip')} {verb} a fix", FIX_GUIDANCE


def stage_bar_html(card: SourceCard) -> str:
    """Eleven segments. The label names every stage and state, so colour is not alone."""
    words = [
        segment_word(segment, index == card.current)
        for index, segment in enumerate(card.segments)
    ]
    label = "Stage progress: " + ", ".join(
        f"{segment.label} {word}" for segment, word in zip(card.segments, words)
    )
    spans = "".join(f'<span class="seg {word}"></span>' for word in words)
    return (
        f'<div class="stage-bar" role="img" aria-label="{escape(label, quote=True)}">'
        f"{spans}</div>"
    )


def _bar_meta(card: SourceCard) -> str:
    segment = card.segments[card.current]
    cost = "paid" if segment.key in PAID_STAGE_KEYS else "free"
    stage = (
        f"Stage {card.current + 1} of {len(card.segments)} · "
        f"{escape(segment.label)} · {cost} stage"
    )
    budget = (
        f"Source budget {_money(card.cost)} of {_money(card.ceiling)} · "
        f"{_money(card.left)} left"
    )
    return f'<p class="bar-meta"><span>{stage}</span><span>{budget}</span></p>'


def _next_html(row: BoardRow, ctx: BoardContext, sentence: tuple[str, str]) -> str:
    bold, muted = sentence
    detail = f'<span class="secondary">{escape(muted)}</span>' if muted else ""
    return (
        f'<div class="next-step"><p class="next-line"><strong>{escape(bold)}</strong>'
        f'{detail}</p><div class="next-action">{_action_html(row, ctx)}</div></div>'
    )


def card_html(card: SourceCard, row: BoardRow, ctx: BoardContext) -> str:
    link = f"{BASE_PATH}/sources/{card.source_id}"
    sentence = _sentence(card, row)
    return (
        f'<li class="card"><div class="thumb" aria-hidden="true">'
        f"{escape(row.snapshot.duration)}</div>"
        '<div class="card-body"><div class="card-head">'
        f'<h3><a href="{link}">{escape(card.title)}</a></h3>'
        f'<span class="secondary">#{card.source_id} · {escape(card.niche)}</span>'
        f"{badge(card.chip.label, card.chip.tone)}</div>"
        f"{stage_bar_html(card)}{_bar_meta(card)}{_notes_html(row.cells, sentence[1])}"
        f"{_next_html(row, ctx, sentence)}</div></li>"
    )


def legend_html() -> str:
    items = "".join(
        f'<li><span class="seg {word.lower()}" aria-hidden="true"></span>{word}</li>'
        for word in _LEGEND
    )
    return (
        f'<ul class="legend" aria-label="Stage legend"><li>{len(STAGES)} stages</li>'
        f"{items}</ul>"
    )


def cards_html(
    view: BoardView, rows: list[BoardRow], ctx: BoardContext, visible: list[int]
) -> str:
    """The cards of the visible sources, in board order, then the legend."""
    by_id = {row.snapshot.source_id: row for row in rows}
    wanted = set(visible)
    items = "".join(
        card_html(card, by_id[card.source_id], ctx)
        for card in view.cards
        if card.source_id in wanted
    )
    return f'<ol class="cards" aria-label="Active runs">{items}</ol>{legend_html()}'
