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
from montaj.dashboard_detail import live_panel_html, render_detail
from montaj.dashboard_new_job import new_job_html
from montaj.dashboard_parts import (
    BASE_PATH,
    JOBS_LINK,
    NEW_JOB_LINK,
    STATE_LABELS,
    BoardContext,
    LiveJob,
    badge,
    busy_html,
    hidden,
    link_button,
    parse_source_id,
    render_dashboard_page,
)
from montaj.db import connect, fetch
from montaj.housekeeping import disk_free
from montaj.ingest import SourceMetadata, source_dir
from montaj.models import ClipStatus, Source

if TYPE_CHECKING:
    from montaj.board import BoardRow, StageCell

_CHECKPOINTS = (
    ("Transcript", "transcript.json"),
    ("Candidates", "candidates.json"),
    ("Ranking", "rank.json"),
)


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


def _cell_html(cell: "StageCell") -> str:
    state = cell.state.value
    return (
        f'<li class="cell {state}"><span class="cell-name">{escape(cell.label)}</span>'
        f'<span class="cell-state">{STATE_LABELS[state]}</span></li>'
    )


def _notes_html(cells: tuple["StageCell", ...]) -> str:
    """Each distinct failure or blocked reason once, in strip order."""
    reasons = dict.fromkeys(cell.reason for cell in cells if cell.reason)
    if not reasons:
        return ""
    items = "".join(f"<li>{escape(reason)}</li>" for reason in reasons)
    return f'<ul class="cell-notes">{items}</ul>'


def _resume_html(source_id: int, label: str, ctx: BoardContext) -> str:
    if ctx.running is not None:
        return busy_html(label, ctx.running)
    return (
        f'<form class="row-action" action="{JOBS_LINK}" method="post">'
        f"{hidden(token=ctx.token, resume=source_id)}"
        f'<button class="button primary">{escape(label)}</button></form>'
    )


def _action_html(row: "BoardRow", ctx: BoardContext) -> str:
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
    return link_button(target, action.label)


def _row_html(row: "BoardRow", ctx: BoardContext) -> str:
    source = row.snapshot
    strip = "".join(_cell_html(cell) for cell in row.cells)
    return (
        f'<tr><th scope="row"><a href="{BASE_PATH}/sources/{source.source_id}">{escape(source.title)}</a>'
        f'<span class="secondary">#{source.source_id} · {source.niche}</span>'
        f"{badge(source.state, source.tone)}</th>"
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


def _empty_html(has_sources: bool, ctx: BoardContext) -> str:
    if has_sources:
        return '<div class="empty"><h2>No sources match this view</h2><a href="/dashboard">Show all sources</a></div>'
    start = (
        f'Start with <a href="{NEW_JOB_LINK}">New job</a> or import'
        if ctx.operations
        else "Import"
    )
    return (
        f'<div class="empty"><h2>No sources yet</h2><p>{start} a permitted video '
        "with <code>montaj ingest</code>.</p></div>"
    )


def _topbar_html(
    sources: list[SourceSnapshot], review_count: int, attention: int, ctx: BoardContext
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
    settings: Settings, board: list["BoardRow"], view: str, ctx: BoardContext
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
        f'<a class="button primary" href="{NEW_JOB_LINK}">New job</a>'
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
{notice}{live_panel_html(ctx.live)}{new_job_html(ctx)}{topbar}
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


def _campaign_choices(settings: Settings) -> tuple[tuple[int, str], ...]:
    with connect(settings.db_path, read_only=True) as db:
        rows = db.execute("SELECT id, name FROM campaigns ORDER BY name").fetchall()
    return tuple((int(row[0]), str(row[1])) for row in rows)


def _campaign_name(settings: Settings, source_id: int) -> str:
    with connect(settings.db_path, read_only=True) as db:
        row = db.execute(
            """SELECT campaigns.name FROM sources
               JOIN campaigns ON campaigns.id = sources.campaign_id
               WHERE sources.id = ?""",
            (source_id,),
        ).fetchone()
    return str(row[0]) if row else ""


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
    choices: tuple[tuple[int, str], ...] = ()
    try:
        from montaj.board import build_rows  # board imports this module

        board = build_rows(settings, running=running, source_id=source_id)
        if source_id is not None:
            campaign = _campaign_name(settings, source_id)
        elif operations:
            choices = _campaign_choices(settings)
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
    context = BoardContext(
        operations,
        running,
        token,
        notice,
        run_spend,
        live,
        campaign,
        ceiling,
        choices,
    )
    if source_id is None:
        return 200, html, _overview(settings, board, view, context)
    if not board:
        return not_found_response()
    return 200, html, render_detail(board[0], context)
