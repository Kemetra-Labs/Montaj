"""Read-only views of source and clip checkpoints."""

import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from html import escape
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError

from montaj.budget import Budget
from montaj.config import Settings
from montaj.costs import cost_totals
from montaj.dashboard_controls import controls_html
from montaj.dashboard_detail import live_panel_html, render_detail
from montaj.dashboard_header import header_html, render_page, summary_html
from montaj.dashboard_new_job import new_job_html
from montaj.dashboard_parts import (
    BASE_PATH,
    BoardContext,
    LiveJob,
    parse_source_id,
    render_board_page,
    source_problem,
)
from montaj.db import connect, fetch
from montaj.housekeeping import disk_free
from montaj.ingest import SourceMetadata, source_dir
from montaj.models import ClipStatus, Source

if TYPE_CHECKING:
    from montaj.board import BoardRow
    from montaj.board_view import BoardView, LockInfo, OutputRecord

_UNREADABLE = '<p class="empty-copy">Recent outputs could not be read.</p>'
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
    tone: str
    problem: str | None
    media: str
    duration: str
    checkpoints: tuple[tuple[str, str], ...]
    clips: Counter[ClipStatus]
    cost: float
    speech_status: str | None = None


def _problem(source: Source) -> str | None:
    if not source.source_allowed:
        return "Record permission for this source before processing it."
    return source_problem(source.speech_status, source.failure_note)


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
        tone = "warning"
    elif clips[ClipStatus.CHECKED]:
        tone = "review"
    elif clips[ClipStatus.APPROVED]:
        tone = "success"
    elif clips[ClipStatus.EXPORTED] or clips[ClipStatus.POSTED]:
        tone = "success"
    elif media == "Imported":
        tone = "success"
    elif media == "Media missing":
        tone = "warning"
    else:
        tone = "neutral"
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
        tone,
        problem,
        media,
        duration,
        checkpoints,
        clips,
        cost,
        source.speech_status,
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


def _empty_html(has_sources: bool, ctx: BoardContext) -> str:
    from montaj.dashboard_states import empty_board_html  # imports the board

    if has_sources:
        return '<div class="empty"><h2>No sources match this view</h2><a href="/dashboard">Show all sources</a></div>'
    return empty_board_html(ctx.operations)


def _job_html(board: list["BoardRow"], ctx: BoardContext, data: "BoardView") -> str:
    """The latest job: the failed panel when it stopped with a problem.

    A batch that ran out of eligible candidates did not fail; it keeps the
    Latest job panel.
    """
    from montaj.dashboard_states import failed_job_html, retry_html
    from montaj.job import NO_MORE_CANDIDATES

    live = ctx.live
    if live is None or live.running or live.problem.strip() in ("", NO_MORE_CANDIDATES):
        return live_panel_html(live)
    titles = {row.snapshot.source_id: row.snapshot.title for row in board}
    card = next((c for c in data.cards if c.source_id == live.source_id), None)
    review = card is not None and card.clips_to_review > 0
    retry = retry_html(live, titles, card, ctx)
    return failed_job_html(live, titles, retry, review)


def _overview(
    settings: Settings,
    board: list["BoardRow"],
    view: str,
    ctx: BoardContext,
    data: "BoardView",
    outputs_failed: bool = False,
) -> bytes:
    from montaj.board_view import awaiting_review, needs_attention  # board imports us
    from montaj.dashboard_cards import cards_html
    from montaj.dashboard_outputs import outputs_html

    attention = [row for row in board if needs_attention(row)]
    review = [row for row in board if awaiting_review(row)]
    views = {
        "all": ("All sources", board),
        "attention": ("Needs attention", attention),
        "review": ("Awaiting review", review),
    }
    view = view if view in views else "all"
    visible = views[view][1]
    filters = "".join(
        f'<a href="/dashboard?view={key}" {"aria-current=page" if key == view else ""}>'
        f"{label} <span>{len(items)}</span></a>"
        for key, (label, items) in views.items()
    )
    try:
        free_bytes: int | None = disk_free(settings.data_dir)
    except OSError:
        free_bytes = None
    results = (
        cards_html(data, board, ctx, [row.snapshot.source_id for row in visible])
        if visible
        else _empty_html(bool(board), ctx)
    )
    # A board notice comes only from a refused New job submission. The form
    # reopens in the controls bar at the bottom, so the notice links to it.
    back = ' <a href="#new-job">Back to the New job form</a>' if ctx.operations else ""
    notice = (
        f'<p class="notice" role="status">{escape(ctx.notice)}{back}</p>'
        if ctx.notice
        else ""
    )
    topbar = summary_html(data.summary)
    return render_board_page(
        "Pipeline Control",
        f"""{header_html(ctx.running is not None)}
{notice}{_job_html(board, ctx, data)}{topbar}
<div class="pipeline">
<section class="queue" aria-labelledby="sources"><div class="panel-head"><h2 id="sources">Active runs</h2>
<nav class="filters" aria-label="Filter sources">{filters}</nav></div>{results}</section>
<aside class="side" aria-label="Recent outputs"><section class="panel recent"><div class="section-heading"><h2>Recent outputs</h2></div>
{_UNREADABLE if outputs_failed else outputs_html(data.outputs, datetime.now())}</section></aside></div>
{controls_html(data.summary, new_job_html(ctx, open_form=not board), view, free_bytes, settings.min_free_mb)}""",
        refresh=ctx.running is not None,
    )


def _outputs(settings: Settings) -> tuple[tuple["OutputRecord", ...], bool]:
    """The newest clips, and whether they could not be read.

    A bad clip row must not take the whole board down, so the panel says so.
    """
    from montaj.board_view import recent_outputs  # board imports us

    try:
        return recent_outputs(settings, 5), False
    except (sqlite3.Error, OSError, ValidationError, ValueError):
        return (), True


def _locks(
    settings: Settings, board: list["BoardRow"], running: tuple[int, str] | None
) -> dict[int, "LockInfo"]:
    """Which sources another command holds. A failed read shows no locks."""
    from montaj.board_view import source_locks  # board imports us

    ids = [row.snapshot.source_id for row in board]
    try:
        return source_locks(settings, ids, running)
    except (OSError, ValueError, RuntimeError):
        return {}


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
        render_page(
            "Source not found",
            '<h1 class="page-title">Source not found</h1>'
            '<p><a href="/dashboard">All sources</a></p>',
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
        from montaj.board_view import build_view

        board = build_rows(settings, running=running, source_id=source_id)
        if source_id is not None:
            campaign = _campaign_name(settings, source_id)
        elif operations and running is None:  # the form is not shown while busy
            choices = _campaign_choices(settings)
    except (sqlite3.Error, OSError, ValidationError, ValueError):
        return (
            503,
            html,
            render_page(
                "Dashboard unavailable",
                '<h1 class="page-title">Dashboard unavailable</h1>'
                "<p>Check the data directory and run <code>montaj db init</code> to initialize or migrate the database.</p>",
            ),
        )
    outputs, outputs_failed = _outputs(settings) if source_id is None else ((), False)
    ceilings = Budget.from_settings(settings, None)
    context = BoardContext(
        operations,
        running,
        token,
        notice,
        run_spend,
        live,
        campaign,
        ceilings.per_source,
        choices,
        ceilings.per_run,
    )
    if source_id is None:
        data = build_view(
            board,
            source_ceiling=ceilings.per_source,
            run_ceiling=ceilings.per_run,
            run_spend=run_spend,
            outputs=outputs,
            locks=_locks(settings, board, running),
        )
        return (
            200,
            html,
            _overview(settings, board, view, context, data, outputs_failed),
        )
    if not board:
        return not_found_response()
    return 200, html, render_detail(board[0], context)
