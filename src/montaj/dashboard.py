"""Read-only views of source and clip checkpoints."""

import sqlite3
from collections import Counter
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError

from montaj.budget import Budget
from montaj.config import Settings
from montaj.costs import cost_totals
from montaj.db import connect, fetch
from montaj.housekeeping import disk_free
from montaj.ingest import SourceMetadata, source_dir
from montaj.models import ClipStatus, Source

if TYPE_CHECKING:
    from montaj.board import BoardRow, StageCell
    from montaj.stage_jobs import BoardStage

BASE_PATH = "/dashboard"
STAGE_PATH = f"{BASE_PATH}/stage"
_JOBS_LINK = f"{BASE_PATH}/jobs"
_CONFIRM_SUFFIX = "/confirm"
_REFRESH_SECONDS = 3
_STATE_LABELS = {
    "done": "Done",
    "running": "Running",
    "blocked": "Blocked",
    "failed": "Failed",
    "pending": "Pending",
}
_CHECKPOINTS = (
    ("Transcript", "transcript.json"),
    ("Candidates", "candidates.json"),
    ("Ranking", "rank.json"),
)
_CLIP_LABELS = {
    ClipStatus.CHECKED: "Awaiting review",
    ClipStatus.NEEDS_FIX: "Needs fixes",
}


def parse_source_id(value: str) -> int | None:
    if len(value) <= 19 and value.isascii() and value.isdecimal():
        number = int(value)
        if 0 < number <= 2**63 - 1:
            return number
    return None


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    source_id: int
    title: str
    niche: str
    added: str
    allowed: bool
    basis: str | None
    state: str
    tone: str
    problem: str | None
    media: str
    duration: str
    checkpoints: tuple[tuple[str, str], ...]
    clips: Counter[ClipStatus]
    cost: float


def _problem(source: Source) -> str | None:
    if not source.source_allowed:
        return "Record permission for this source before processing it."
    if source.speech_status == "language_mismatch":
        return "The transcript language differs from the requested language. Check the transcription settings."
    if source.speech_status == "no_usable_speech":
        return "The source has insufficient usable speech. Check the source audio."
    if source.speech_status != "failed":
        return None
    # Historical notes may contain raw tool output or signed URLs. Render fixed guidance.
    note = (source.failure_note or "").casefold()
    if "javascript runtime" in note:
        return "Install Deno or rebuild the Montaj image, then run montaj doctor."
    if "ejs solver" in note:
        return "Restore the matching EJS solver or rebuild the Montaj image, then run montaj doctor."
    if "format" in note and "available" in note:
        return "No usable media format was found. Check the source or obtain a permitted copy."
    return "Montaj recorded a source failure. Check the locally reported error before retrying."


def _artifact(root: Path, name: str) -> Path | None:
    path = root / name
    return path if path.is_file() and not path.is_symlink() else None


def _media(root: Path) -> tuple[str, str]:
    path = _artifact(root, "meta.json")
    if path is None:
        return "Pending", ""
    try:
        metadata = SourceMetadata.model_validate_json(path.read_bytes())
    except (OSError, ValidationError):
        return "Metadata unreadable", ""
    minutes, seconds = divmod(int(metadata.duration), 60)
    duration = f"{minutes}m {seconds:02d}s" if minutes else f"{seconds}s"
    ready = _artifact(root, "video.mp4") and _artifact(root, "audio.wav")
    return ("Imported" if ready else "Media missing"), duration


def _snapshot(
    settings: Settings, source: Source, clips: Counter[ClipStatus], cost: float
) -> SourceSnapshot:
    assert source.id is not None
    root = source_dir(settings, source.id)
    inside = root.resolve().is_relative_to(settings.data_dir.resolve())
    media, duration = _media(root) if inside else ("Media unavailable", "")
    checkpoints = tuple(
        (label, "Saved" if inside and _artifact(root, filename) else "Pending")
        for label, filename in _CHECKPOINTS
    )
    problem = _problem(source)
    if (
        problem
        or clips[ClipStatus.NEEDS_FIX]
        or media in {"Metadata unreadable", "Media unavailable"}
    ):
        state, tone = "Needs attention", "warning"
    elif clips[ClipStatus.CHECKED]:
        state, tone = "Awaiting review", "review"
    elif clips[ClipStatus.APPROVED]:
        state, tone = "Approved", "success"
    elif clips[ClipStatus.EXPORTED] or clips[ClipStatus.POSTED]:
        state, tone = "Exported or posted", "success"
    elif media == "Imported":
        state, tone = "Imported", "success"
    elif media == "Media missing":
        state, tone = media, "warning"
    else:
        state, tone = "Awaiting ingest", "neutral"
    title = source.title.strip()
    if not title or title in {source.url, source.path} or "://" in title:
        title = f"Source {source.id}"
    return SourceSnapshot(
        source.id,
        title,
        source.niche.value.title(),
        source.added_at.strftime("%Y-%m-%d"),
        source.source_allowed,
        source.allowed_basis,
        state,
        tone,
        problem,
        media,
        duration,
        checkpoints,
        clips,
        cost,
    )


def _load(settings: Settings, source_id: int | None) -> list[SourceSnapshot]:
    with connect(settings.db_path, read_only=True) as db:
        if source_id is None:
            rows = db.execute("SELECT * FROM sources ORDER BY id DESC").fetchall()
            sources = [Source.model_validate(dict(row)) for row in rows]
        else:
            source = fetch(db, Source, source_id)
            sources = [source] if source is not None else []
        clip_counts: dict[int, Counter[ClipStatus]] = {}
        for row in db.execute(
            """SELECT source_id, status, COUNT(*) FROM clips
               WHERE (? IS NULL OR source_id = ?) GROUP BY source_id, status""",
            (source_id, source_id),
        ):
            clip_counts.setdefault(row[0], Counter())[ClipStatus(row[1])] = row[2]
        spend = cost_totals(db, source_id).by_source
    return [
        _snapshot(
            settings,
            source,
            clip_counts.get(source.id, Counter()),
            spend.get(source.id, 0),
        )
        for source in sources
        if source.id is not None
    ]


def render_dashboard_page(
    title: str, content: str, active: str = "", refresh: bool = False
) -> bytes:
    reload = (
        f'<meta http-equiv="refresh" content="{_REFRESH_SECONDS}">' if refresh else ""
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">{reload}
<title>{escape(title)} · Montaj</title><link rel="stylesheet" href="/dashboard/style.css"></head>
<body><a class="skip" href="#main">Skip to content</a><div class="shell"><header class="rail">
<a class="brand" href="/dashboard"><span class="brand-mark" aria-hidden="true">M</span>Montaj</a>
<nav aria-label="Main"><a href="/dashboard" {active}>Production board</a><a href="/">Clip preview <span aria-hidden="true">↗</span></a></nav>
</header><main class="frame" id="main">{content}</main></div></body></html>""".encode()


def _badge(text: str, tone: str) -> str:
    return f'<span class="badge {tone}">{escape(text)}</span>'


@dataclass(frozen=True, slots=True)
class LiveJob:
    """A snapshot of the dashboard's current or latest background job."""

    source_id: int | None
    stage: str
    running: bool
    elapsed_s: float
    spend: float | None
    summary: str
    problem: str


@dataclass(frozen=True, slots=True)
class _BoardContext:
    """What the board needs besides the rows: live job state and write access."""

    operations: bool = False
    running: tuple[int, str] | None = None
    token: str = ""
    notice: str = ""
    run_spend: tuple[float, float] | None = None
    live: LiveJob | None = None
    campaign: str = ""
    source_ceiling: float = 0.0


def _hidden(**fields: str | int) -> str:
    return "".join(
        f'<input type="hidden" name="{name}" value="{escape(str(value), quote=True)}">'
        for name, value in fields.items()
    )


def _link_button(href: str, label: str) -> str:
    """A primary link. The caller passes an href that is already HTML-safe."""
    return f'<a class="button primary" href="{href}">{escape(label)}</a>'


def _busy_html(label: str, running: tuple[int, str], describe: str = "") -> str:
    """A disabled button and why. `describe` links the two for screen readers."""
    reason = "Another job is running"
    if running[0]:
        reason += f" (source {running[0]})"
    by = f' aria-describedby="{describe}"' if describe else ""
    anchor = f' id="{describe}"' if describe else ""
    return (
        f'<button disabled class="button"{by}>{escape(label)}</button>'
        f'<span class="secondary"{anchor}>{reason}</span>'
    )


def _cell_html(cell: "StageCell") -> str:
    state = cell.state.value
    return (
        f'<li class="cell {state}"><span class="cell-name">{escape(cell.label)}</span>'
        f'<span class="cell-state">{_STATE_LABELS[state]}</span></li>'
    )


def _notes_html(cells: tuple["StageCell", ...]) -> str:
    """Each distinct failure or blocked reason once, in strip order."""
    reasons = dict.fromkeys(cell.reason for cell in cells if cell.reason)
    if not reasons:
        return ""
    items = "".join(f"<li>{escape(reason)}</li>" for reason in reasons)
    return f'<ul class="cell-notes">{items}</ul>'


def _resume_html(source_id: int, label: str, ctx: _BoardContext) -> str:
    if ctx.running is not None:
        return _busy_html(label, ctx.running)
    return (
        f'<form class="row-action" action="{_JOBS_LINK}" method="post">'
        f"{_hidden(token=ctx.token, resume=source_id)}"
        f'<button class="button primary">{escape(label)}</button></form>'
    )


def _action_html(row: "BoardRow", ctx: _BoardContext) -> str:
    action, source_id = row.action, row.snapshot.source_id
    if action is None:
        return ""
    if action.kind == "resume":
        return _resume_html(source_id, action.label, ctx) if ctx.operations else ""
    target = {
        "review": f"/?source={source_id}",
        "export": f"/?source={source_id}&amp;ready=1",
        "fix": f"{BASE_PATH}/sources/{source_id}",
    }[action.kind]
    return _link_button(target, action.label)


def _row_html(row: "BoardRow", ctx: _BoardContext) -> str:
    source = row.snapshot
    strip = "".join(_cell_html(cell) for cell in row.cells)
    return (
        f'<tr><th scope="row"><a href="{BASE_PATH}/sources/{source.source_id}">{escape(source.title)}</a>'
        f'<span class="secondary">#{source.source_id} · {source.niche}</span>'
        f"{_badge(source.state, source.tone)}</th>"
        f'<td><ol class="strip" aria-label="Stage progress">{strip}</ol>{_notes_html(row.cells)}</td>'
        f'<td class="numeric optional">{source.clips[ClipStatus.CHECKED]}</td>'
        f'<td class="numeric optional">${source.cost:.6f}</td>'
        f'<td class="action-cell">{_action_html(row, ctx)}</td></tr>'
    )


def _table_html(scope: str, rows: str) -> str:
    return f"""<div class="table-wrap" tabindex="0" role="region" aria-label="Source table"><table>
<caption class="sr-only">{scope}</caption><thead><tr><th scope="col">Source</th><th scope="col">Stages</th>
<th scope="col" class="numeric optional">Review clips</th><th scope="col" class="numeric optional">Est. API cost</th><th scope="col">Next</th>
</tr></thead><tbody>{rows}</tbody></table></div>"""


def _empty_html(has_sources: bool, ctx: _BoardContext) -> str:
    if has_sources:
        return '<div class="empty"><h2>No sources match this view</h2><a href="/dashboard">Show all sources</a></div>'
    start = (
        f'Start with <a href="{_JOBS_LINK}">New job</a> or import'
        if ctx.operations
        else "Import"
    )
    return (
        f'<div class="empty"><h2>No sources yet</h2><p>{start} a permitted video '
        "with <code>montaj ingest</code>.</p></div>"
    )


def _topbar_html(
    sources: list[SourceSnapshot], review_count: int, attention: int, ctx: _BoardContext
) -> str:
    live = ""
    if ctx.run_spend is not None:
        spent, ceiling = ctx.run_spend
        live = f"<div><dt>Run spend</dt><dd>${spent:.6f} of ${ceiling:.2f} run ceiling</dd></div>"
    return (
        '<dl class="topbar panel" aria-label="Production summary">'
        f"<div><dt>Est. source API spend</dt><dd>${sum(source.cost for source in sources):.6f}</dd></div>"
        f"{live}<div><dt>Needs attention</dt><dd>{attention}</dd></div>"
        f"<div><dt>Clips awaiting review</dt><dd>{review_count}</dd></div></dl>"
    )


def _overview(
    settings: Settings, board: list["BoardRow"], view: str, ctx: _BoardContext
) -> bytes:
    attention = [row for row in board if row.snapshot.tone == "warning"]
    review = [
        row
        for row in board
        if row.snapshot.allowed and row.snapshot.clips[ClipStatus.CHECKED]
    ]
    views = {
        "all": ("All sources", board),
        "attention": ("Needs attention", attention),
        "review": ("Awaiting review", review),
    }
    view = view if view in views else "all"
    scope, visible = views[view]
    review_count = sum(row.snapshot.clips[ClipStatus.CHECKED] for row in review)
    filters = "".join(
        f'<a href="/dashboard?view={key}" {"aria-current=page" if key == view else ""}>'
        f"{label} <span>{len(items)}</span></a>"
        for key, (label, items) in views.items()
    )
    try:
        space = f"{disk_free(settings.data_dir) / 1024**3:.1f} GB free"
    except OSError:
        space = "Disk space unavailable"
    results = (
        _table_html(scope, "".join(_row_html(row, ctx) for row in visible))
        if visible
        else _empty_html(bool(board), ctx)
    )
    new_job = (
        f'<a class="button primary" href="{_JOBS_LINK}">New job</a>'
        if ctx.operations
        else ""
    )
    notice = (
        f'<p class="notice" role="status">{escape(ctx.notice)}</p>'
        if ctx.notice
        else ""
    )
    topbar = _topbar_html(
        [row.snapshot for row in board], review_count, len(attention), ctx
    )
    return render_dashboard_page(
        "Operations",
        f"""<div class="heading"><h1>Production board</h1><div class="toolbar">{new_job}<a class="button" href="/dashboard?view={view}">Refresh</a></div></div>
{notice}{topbar}
<div class="board">
<section class="queue" aria-labelledby="sources"><div class="panel-head"><h2 id="sources">Source queue</h2>
<nav class="filters" aria-label="Filter sources">{filters}</nav></div>{results}</section>
<aside class="side" aria-label="Workload"><section class="panel workload"><div class="section-heading"><h2>Workload</h2></div>
<a class="work-item" href="/dashboard?view=attention"><span><span class="signal warning" aria-hidden="true"></span>Needs attention</span><strong>{len(attention)}</strong></a>
<a class="work-item" href="/dashboard?view=review"><span><span class="signal review" aria-hidden="true"></span>Clips awaiting review</span><strong>{review_count}</strong></a>
</section><section class="panel resources"><div class="section-heading"><h2>Resources</h2></div>
<dl><div><dt>Data volume</dt><dd>{space}</dd></div></dl></section></aside></div>""",
        'aria-current="page"',
        refresh=ctx.running is not None,
    )


def _campaign_name(settings: Settings, source_id: int) -> str:
    with connect(settings.db_path, read_only=True) as db:
        row = db.execute(
            """SELECT campaigns.name FROM sources
               JOIN campaigns ON campaigns.id = sources.campaign_id
               WHERE sources.id = ?""",
            (source_id,),
        ).fetchone()
    return str(row[0]) if row else ""


def confirm_source_id(path: str) -> int | None:
    """The source ID of a `/dashboard/sources/<id>/confirm` path, else None."""
    prefix = f"{BASE_PATH}/sources/"
    if not (path.startswith(prefix) and path.endswith(_CONFIRM_SUFFIX)):
        return None
    return parse_source_id(path.removeprefix(prefix).removesuffix(_CONFIRM_SUFFIX))


def _detail_path(source_id: int) -> str:
    return f"{BASE_PATH}/sources/{source_id}"


def _stage_action_html(cell: "StageCell", source_id: int, ctx: _BoardContext) -> str:
    """Run for a pending stage and Retry for a failed one. Paid stages confirm cost first."""
    from montaj.stage_jobs import PAID_STAGES, BoardStage  # imports this module

    if not ctx.operations or cell.state.value not in {"pending", "failed"}:
        return ""
    try:
        stage = BoardStage(cell.key)
    except ValueError:
        return ""  # ingest, review and export are not single-stage jobs
    retry = cell.state.value == "failed"
    label = "Retry" if retry else "Run"
    if ctx.running is not None:
        return _busy_html(label, ctx.running, f"why-{cell.key}")
    if stage in PAID_STAGES:
        href = f"{_detail_path(source_id)}{_CONFIRM_SUFFIX}?stage={stage.value}"
        return _link_button(
            escape(href + ("&retry=yes" if retry else "")), f"{label} (confirm cost)"
        )
    fields = _hidden(token=ctx.token, source=source_id, stage=stage.value)
    if retry:
        fields += _hidden(retry="yes")
    return (
        f'<form class="row-action" action="{STAGE_PATH}" method="post">{fields}'
        f'<button class="button primary">{label}</button></form>'
    )


def _stage_line_html(row: "BoardRow", cell: "StageCell", ctx: _BoardContext) -> str:
    source = row.snapshot
    state = cell.state.value
    reason = (
        f'<span class="secondary">{escape(cell.reason)}</span>'
        if cell.reason and state in {"blocked", "failed"}
        else ""
    )
    action = _stage_action_html(cell, source.source_id, ctx)
    if cell.key == "review" and source.allowed and source.clips[ClipStatus.CHECKED]:
        action = _link_button(f"/?source={source.source_id}", "Open review")
    return (
        f'<li class="stage-line {state}"><div class="stage-info">'
        f'<span class="cell-name">{escape(cell.label)}</span>'
        f'<span class="cell-state">{_STATE_LABELS[state]}</span>{reason}</div>'
        f'<div class="stage-action">{action}</div></li>'
    )


def _stages_html(row: "BoardRow", ctx: _BoardContext) -> str:
    lines = "".join(_stage_line_html(row, cell, ctx) for cell in row.cells)
    return (
        '<section class="panel"><div class="section-heading"><h2>Stages</h2></div>'
        f'<ol class="steps stage-lines">{lines}</ol></section>'
    )


def _live_html(live: LiveJob | None, source_id: int) -> str:
    """The job panel, shown only for the source the job is working on."""
    if live is None or live.source_id != source_id:
        return ""
    spend = f"${live.spend:.6f}" if live.spend is not None else "unavailable"
    figures = f"<p>Elapsed {live.elapsed_s:.0f} s · Run spend {spend}</p>"
    if live.running:
        heading, body = f"Running: {live.stage}", ""
    else:
        heading, body = "Latest job", f"<p>{escape(live.summary)}</p>"
    problem = f"<p>{escape(live.problem)}</p>" if live.problem else ""
    return (
        '<section class="panel job-panel" aria-label="Live job">'
        f"<h2>{escape(heading)}</h2>{body}{figures}{problem}</section>"
    )


def _preview_link(source: SourceSnapshot) -> str:
    reviewable = (
        ClipStatus.CAPTIONED,
        ClipStatus.DRAFTED,
        ClipStatus.CHECKED,
        ClipStatus.NEEDS_FIX,
        ClipStatus.APPROVED,
        ClipStatus.EXPORTED,
    )
    if not (source.allowed and any(source.clips[state] for state in reviewable)):
        return ""
    return (
        f'<a class="button primary" href="/?source={source.source_id}">'
        'Preview clips <span aria-hidden="true">↗</span></a>'
    )


def _detail_header(source: SourceSnapshot, ctx: _BoardContext) -> str:
    campaign = f" · Campaign: {escape(ctx.campaign)}" if ctx.campaign else ""
    spend = (
        f"Source spend ${source.cost:.6f} of ${ctx.source_ceiling:.2f} source ceiling"
    )
    return (
        f'<div class="heading"><div><div class="eyebrow">Source #{source.source_id} · {source.niche}{campaign}</div>'
        f'<h1>{escape(source.title)}</h1><p class="secondary">{spend}</p></div>'
        f'<div class="toolbar">{_badge(source.state, source.tone)}'
        f'<a class="button" href="{_detail_path(source.source_id)}">Refresh</a></div></div>'
    )


def _detail(row: "BoardRow", ctx: _BoardContext) -> bytes:
    source = row.snapshot
    steps = (("Ingest", source.media), *source.checkpoints)
    checkpoints = "".join(
        f"<li><span>{label}</span>{_badge(value, 'success' if value in {'Imported', 'Saved'} else 'neutral' if value == 'Pending' else 'warning')}</li>"
        for label, value in steps
    )
    clips = "".join(
        f"<li><span>{_CLIP_LABELS.get(state, state.value.replace('_', ' ').capitalize())}</span><strong>{count}</strong></li>"
        for state in ClipStatus
        if (count := source.clips[state])
    )
    notice = (
        f'<aside class="notice"><strong>Needs attention</strong><p>{escape(source.problem)}</p></aside>'
        if source.problem
        else ""
    )
    basis = (
        escape(source.basis or "Basis not recorded")
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
{notice}<div class="board detail">
<div class="side">{_live_html(ctx.live, source.source_id)}{_stages_html(row, ctx)}
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
    fields = _hidden(token=token, source=source_id, stage=stage.value, confirm="yes")
    if retry:
        fields += _hidden(retry="yes")
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


def not_found_response() -> tuple[int, str, bytes]:
    return (
        404,
        "text/html; charset=utf-8",
        render_dashboard_page(
            "Source not found",
            '<h1>Source not found</h1><a href="/dashboard">All sources</a>',
        ),
    )


def dashboard_response(
    settings: Settings,
    path: str,
    view: str,
    *,
    operations: bool = False,
    running: tuple[int, str] | None = None,
    token: str = "",
    notice: str = "",
    run_spend: tuple[float, float] | None = None,
    live: LiveJob | None = None,
) -> tuple[int, str, bytes]:
    html = "text/html; charset=utf-8"
    if path == f"{BASE_PATH}/style.css":
        return (
            200,
            "text/css; charset=utf-8",
            Path(__file__).with_name("dashboard.css").read_bytes(),
        )
    if path in (BASE_PATH, f"{BASE_PATH}/"):
        source_id = None
    else:
        prefix = f"{BASE_PATH}/sources/"
        source_id = (
            parse_source_id(path.removeprefix(prefix))
            if path.startswith(prefix)
            else None
        )
        if source_id is None:
            return not_found_response()
    board: list[BoardRow] = []
    campaign = ""
    try:
        from montaj.board import build_rows  # board imports this module

        board = build_rows(settings, running=running, source_id=source_id)
        if source_id is not None:
            campaign = _campaign_name(settings, source_id)
    except (sqlite3.Error, OSError, ValidationError, ValueError):
        return (
            503,
            html,
            render_dashboard_page(
                "Dashboard unavailable",
                "<h1>Dashboard unavailable</h1><p>Check the data directory and run <code>montaj db init</code> to initialize or migrate the database.</p>",
            ),
        )
    ceiling = Budget.from_settings(settings, None).per_source
    context = _BoardContext(
        operations, running, token, notice, run_spend, live, campaign, ceiling
    )
    if source_id is None:
        return 200, html, _overview(settings, board, view, context)
    if not board:
        return not_found_response()
    return 200, html, _detail(board[0], context)
