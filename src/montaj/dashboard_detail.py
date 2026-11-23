"""The source detail pane, the cost confirmation page and the live job panel."""

from html import escape
from typing import TYPE_CHECKING

from montaj.dashboard_parts import (
    BASE_PATH,
    PERMISSION_REASONS,
    STAGE_PATH,
    STATE_LABELS,
    BoardContext,
    LiveJob,
    badge,
    busy_html,
    hidden,
    link_button,
    parse_source_id,
    permission_reason,
    render_dashboard_page,
)
from montaj.models import ClipStatus

if TYPE_CHECKING:
    from montaj.board import BoardRow, StageCell
    from montaj.dashboard import SourceSnapshot
    from montaj.stage_jobs import BoardStage

_CONFIRM_SUFFIX = "/confirm"

_CLIP_LABELS = {
    ClipStatus.CHECKED: "Awaiting review",
    ClipStatus.NEEDS_FIX: "Needs fixes",
}


def confirm_source_id(path: str) -> int | None:
    """The source ID of a `/dashboard/sources/<id>/confirm` path, else None."""
    prefix = f"{BASE_PATH}/sources/"
    if not (path.startswith(prefix) and path.endswith(_CONFIRM_SUFFIX)):
        return None
    return parse_source_id(path.removeprefix(prefix).removesuffix(_CONFIRM_SUFFIX))


def _detail_path(source_id: int) -> str:
    return f"{BASE_PATH}/sources/{source_id}"


_ACTION_LABELS = {"pending": "Run", "failed": "Retry", "done": "Rerun"}


def _runnable_stage(cell: "StageCell") -> "BoardStage | None":
    """The board stage a cell runs as one job. None for ingest, review and export."""
    from montaj.stage_jobs import BoardStage  # imports this module

    try:
        return BoardStage(cell.key)
    except ValueError:
        return None


def _stage_action_html(cell: "StageCell", source_id: int, ctx: BoardContext) -> str:
    """Run, Retry or Rerun a stage. Paid stages confirm cost first.

    Rerun reuses saved output while the stage inputs are unchanged.
    """
    from montaj.stage_jobs import PAID_STAGES  # imports this module

    state = cell.state.value
    stage = _runnable_stage(cell)
    if not ctx.operations or stage is None or state not in _ACTION_LABELS:
        return ""
    label = _ACTION_LABELS[state]
    if ctx.running is not None:
        return busy_html(label, ctx.running, f"why-{cell.key}")
    retry = state == "failed"
    if stage in PAID_STAGES:
        href = f"{_detail_path(source_id)}{_CONFIRM_SUFFIX}?stage={stage.value}"
        text = label if state == "done" else f"{label} (confirm cost)"
        return link_button(escape(href + ("&retry=yes" if retry else "")), text)
    fields = hidden(token=ctx.token, source=source_id, stage=stage.value)
    if retry:
        fields += hidden(retry="yes")
    return (
        f'<form class="row-action" action="{STAGE_PATH}" method="post">{fields}'
        f'<button class="button primary">{label}</button></form>'
    )


def _stage_note(cell: "StageCell") -> str:
    """Up to date for a finished stage. The reason for a blocked or failed one.

    A permission reason is shown once as the page notice, not on every line.
    """
    state = cell.state.value
    if state == "done" and _runnable_stage(cell) is not None:
        return '<span class="secondary">Up to date</span>'
    if (
        cell.reason
        and state in {"blocked", "failed"}
        and cell.reason not in PERMISSION_REASONS
    ):
        return f'<span class="secondary">{escape(cell.reason)}</span>'
    return ""


def _stage_line_html(row: "BoardRow", cell: "StageCell", ctx: BoardContext) -> str:
    source = row.snapshot
    state = cell.state.value
    permitted = not permission_reason(source.allowed, source.basis)
    action = _stage_action_html(cell, source.source_id, ctx)
    if cell.key == "review" and permitted and source.clips[ClipStatus.CHECKED]:
        action = link_button(f"/?source={source.source_id}", "Open review")
    return (
        f'<li class="stage-line {state}"><div class="stage-info">'
        f'<span class="cell-name">{escape(cell.label)}</span>'
        f'<span class="cell-state">{STATE_LABELS[state]}</span>'
        f"{_stage_note(cell)}</div>"
        f'<div class="stage-action">{action}</div></li>'
    )


def _stages_html(row: "BoardRow", ctx: BoardContext) -> str:
    lines = "".join(_stage_line_html(row, cell, ctx) for cell in row.cells)
    return (
        '<section class="panel"><div class="section-heading"><h2>Stages</h2></div>'
        f'<ol class="steps stage-lines">{lines}</ol></section>'
    )


def live_panel_html(live: LiveJob | None, source_id: int | None = None) -> str:
    """The job panel. A source page passes its ID and sees only its own job.

    The board home passes none and sees the current or latest job of any source.
    """
    if live is None or (source_id is not None and live.source_id != source_id):
        return ""
    spend = f"${live.spend:.6f}" if live.spend is not None else "unavailable"
    figures = f"<p>Elapsed {live.elapsed_s:.0f} s · Run spend {spend}</p>"
    if live.running:
        heading, body = f"Running: {live.stage}", ""
    else:
        heading, body = "Latest job", f"<p>{escape(live.summary)}</p>"
    problem = f"<p>{escape(live.problem)}</p>" if live.problem else ""
    review = ""
    if live.source_id is not None and not live.running and not live.problem:
        review = (
            f'<p><a href="/?source={live.source_id}&amp;ready=1">Review clips</a></p>'
        )
    return (
        '<section class="panel job-panel" aria-label="Live job">'
        f"<h2>{escape(heading)}</h2>{body}{figures}{problem}{review}</section>"
    )


def _preview_link(source: "SourceSnapshot") -> str:
    reviewable = (
        ClipStatus.CAPTIONED,
        ClipStatus.DRAFTED,
        ClipStatus.CHECKED,
        ClipStatus.NEEDS_FIX,
        ClipStatus.APPROVED,
        ClipStatus.EXPORTED,
    )
    permitted = not permission_reason(source.allowed, source.basis)
    if not (permitted and any(source.clips[state] for state in reviewable)):
        return ""
    return (
        f'<a class="button primary" href="/?source={source.source_id}">'
        'Preview clips <span aria-hidden="true">↗</span></a>'
    )


def _detail_header(source: "SourceSnapshot", ctx: BoardContext) -> str:
    campaign = f" · Campaign: {escape(ctx.campaign)}" if ctx.campaign else ""
    spend = (
        f"Source spend ${source.cost:.6f} of ${ctx.source_ceiling:.2f} source ceiling"
    )
    return (
        f'<div class="heading"><div><div class="eyebrow">Source #{source.source_id} · {source.niche}{campaign}</div>'
        f'<h1>{escape(source.title)}</h1><p class="secondary">{spend}</p></div>'
        f'<div class="toolbar">{badge(source.state, source.tone)}'
        f'<a class="button" href="{_detail_path(source.source_id)}">Refresh</a></div></div>'
    )


def _notice_html(source: "SourceSnapshot") -> str:
    """What needs attention on this source, each condition worded once."""
    basis_missing = (
        permission_reason(source.allowed, source.basis) if source.allowed else ""
    )
    messages = [text for text in (source.problem, basis_missing) if text]
    if not messages:
        return ""
    body = "".join(f"<p>{escape(text)}</p>" for text in messages)
    return f'<aside class="notice"><strong>Needs attention</strong>{body}</aside>'


def render_detail(row: "BoardRow", ctx: BoardContext) -> bytes:
    source = row.snapshot
    steps = (("Ingest", source.media), *source.checkpoints)
    checkpoints = "".join(
        f"<li><span>{label}</span>{badge(value, 'success' if value in {'Imported', 'Saved'} else 'neutral' if value == 'Pending' else 'warning')}</li>"
        for label, value in steps
    )
    clips = "".join(
        f"<li><span>{_CLIP_LABELS.get(state, state.value.replace('_', ' ').capitalize())}</span><strong>{count}</strong></li>"
        for state in ClipStatus
        if (count := source.clips[state])
    )
    basis = (
        escape((source.basis or "").strip() or "Basis not recorded")
        if source.allowed
        else "Permission required"
    )
    clip_list = (
        f'<ul class="steps">{clips}</ul>'
        if clips
        else '<p class="empty-copy">No clips recorded yet.</p>'
    )
    return render_dashboard_page(
        source.title,
        f"""<a class="back" href="/dashboard">← Source queue</a>
{_detail_header(source, ctx)}
{_notice_html(source)}<div class="board detail">
<div class="side">{live_panel_html(ctx.live, source.source_id)}{_stages_html(row, ctx)}
<section class="panel"><div class="section-heading"><h2>Saved checkpoints</h2></div><ol class="steps timeline">{checkpoints}</ol></section></div>
<div class="side"><section class="panel"><div class="section-heading"><h2>Source information</h2></div>
<dl class="facts"><div><dt>Permission</dt><dd>{basis}</dd></div><div><dt>Added</dt><dd>{source.added}</dd></div>
<div><dt>Duration</dt><dd>{source.duration or "Unknown"}</dd></div><div><dt>Est. API cost</dt><dd>${source.cost:.6f}</dd></div></dl></section>
<section class="panel"><div class="section-heading"><h2>Clips</h2>{_preview_link(source)}</div>{clip_list}</section></div></div>""",
        'aria-current="location"',
        refresh=ctx.running is not None,
    )


def render_confirm(
    source_id: int,
    stage: "BoardStage",
    lines: list[str],
    token: str,
    *,
    retry: bool = False,
) -> bytes:
    """The estimate for a paid stage and the one click that accepts its cost."""
    items = "".join(f"<li>{escape(line)}</li>" for line in lines)
    fields = hidden(token=token, source=source_id, stage=stage.value, confirm="yes")
    if retry:
        fields += hidden(retry="yes")
    return render_dashboard_page(
        "Confirm cost",
        f"""<a class="back" href="{_detail_path(source_id)}">← Source #{source_id}</a>
<div class="heading"><h1>Confirm cost</h1></div>
<section class="panel job-panel"><h2>{escape(stage.value.capitalize())} for source #{source_id}</h2>
<ul class="estimate">{items}</ul>
<form class="job-form" action="{STAGE_PATH}" method="post">{fields}
<button class="button primary">Confirm and run</button></form>
<a class="button" href="{_detail_path(source_id)}">Cancel</a></section>""",
    )


def render_confirm_unavailable(source_id: int) -> bytes:
    """No estimate, so nothing to confirm. Only a way back."""
    return render_dashboard_page(
        "Confirm cost",
        f"""<a class="back" href="{_detail_path(source_id)}">← Source #{source_id}</a>
<div class="heading"><h1>Confirm cost</h1></div>
<section class="panel job-panel"><p>Estimate unavailable. Check the source before running this stage.</p>
<a class="button" href="{_detail_path(source_id)}">Back to source</a></section>""",
    )
