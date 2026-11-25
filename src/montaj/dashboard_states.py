"""The board's special states: a failed job, a locked source and an empty board.

Words first: every state says what happened and what to do, never colour alone.
"""

from datetime import datetime
from html import escape

from montaj.board_view import LockInfo, SourceCard, job_problem_note
from montaj.dashboard_parts import (
    BASE_PATH,
    BoardContext,
    LiveJob,
    busy_html,
    format_usd,
    link_button,
)

LOCKED_REASON = "Locked by another command"
OVER_BUDGET_REASON = "Over the source ceiling"
_KEPT = "Saved work is kept. Finished stages are reused when you retry."
_START_AGAIN = "Start it again from New job."
_INGEST = "<code>montaj ingest</code>"


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def lock_age_text(started: datetime | None, now: datetime) -> str:
    """How old a lock is, in words. A missing or future start time is unknown."""
    if started is None or started > now:
        return "The lock age is unknown."
    seconds = (now - started).total_seconds()
    if seconds < 60:
        age = "less than a minute"
    elif seconds < 3600:
        age = _plural(int(seconds // 60), "minute")
    else:
        age = _plural(int(seconds // 3600), "hour")
    return f"This lock is {age} old."


def lock_text(lock: LockInfo, now: datetime) -> str:
    """Who holds a source's lock and how long for. Plain text; the caller escapes."""
    holder = f"Locked by {lock.command}." if lock.command else "Locked."
    return f"{holder} {lock_age_text(lock.started, now)}"


def over_budget_text(card: SourceCard) -> str:
    ceiling = format_usd(card.ceiling)
    return f"Over its {ceiling} source ceiling. Paid stages are off for this source."


def disabled_html(label: str, reason: str, source_id: int) -> str:
    """A disabled paid button and why, linked for screen readers."""
    anchor = f"why-{source_id}"
    return (
        f'<button disabled class="button" aria-describedby="{anchor}">'
        f"{escape(label)}</button>"
        f'<span class="secondary" id="{anchor}">{escape(reason)}</span>'
    )


def withheld_reason(card: SourceCard) -> str:
    """Why a paid Resume or Retry is off for this source, or nothing."""
    if card.lock is not None:
        return LOCKED_REASON
    if card.over_budget:
        return OVER_BUDGET_REASON
    return ""


def retry_html(
    live: LiveJob,
    titles: dict[int, str],
    card: SourceCard | None,
    ctx: BoardContext,
) -> str:
    """Retry for a failed job: the batch cost confirmation, or why it is not offered."""
    source_id = live.source_id
    if source_id is None or source_id not in titles:
        return f"<p>{_START_AGAIN}</p>"
    if not ctx.operations or (card is not None and card.chip.label == "Blocked"):
        return ""
    reason = withheld_reason(card) if card is not None else ""
    if reason:
        return disabled_html("Retry", reason, source_id)
    if ctx.running is not None:
        return busy_html("Retry", ctx.running, source_id=source_id)
    return link_button(f"{BASE_PATH}/sources/{source_id}/confirm?stage=batch", "Retry")


def failed_job_html(live: LiveJob, titles: dict[int, str], retry: str) -> str:
    """The latest job stopped with a problem: what, where, and what is kept."""
    title = titles.get(live.source_id) if live.source_id is not None else None
    where = escape(title) if title else "a new import"
    spend = format_usd(live.spend) if live.spend is not None else "unavailable"
    note = job_problem_note(live.problem)
    return (
        '<section class="panel failed-job" aria-label="Failed job">'
        "<h2>Job failed</h2>"
        f"<p><strong>Stopped during {escape(live.stage)}</strong> on {where}.</p>"
        f"<p>{escape(note)}</p><p>{_KEPT}</p>"
        f'<p class="secondary">Run spend {escape(spend)}</p>'
        f'<div class="next-action">{retry}</div></section>'
    )


def empty_board_html(operations: bool) -> str:
    """A board with no sources teaches the first step."""
    if operations:
        body = (
            f"<p>Start one with New job, or import a permitted video with {_INGEST}.</p>"
            '<a class="button primary" href="#new-job">New job</a>'
        )
    else:
        body = f"<p>Import a permitted video with {_INGEST}, then refresh.</p>"
    return (
        '<section class="empty-board" aria-label="No runs yet">'
        f"<h2>No runs yet</h2>{body}</section>"
    )
