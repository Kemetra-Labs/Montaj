"""Read-only views of source and clip checkpoints."""

import sqlite3
from collections import Counter
from dataclasses import dataclass
from html import escape
from pathlib import Path

from pydantic import ValidationError

from montaj.config import Settings
from montaj.costs import cost_totals
from montaj.db import connect, fetch
from montaj.housekeeping import disk_free
from montaj.ingest import SourceMetadata, source_dir
from montaj.models import ClipStatus, Source

BASE_PATH = "/dashboard"
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


def render_dashboard_page(title: str, content: str, active: str = "") -> bytes:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)} · Montaj</title><link rel="stylesheet" href="/dashboard/style.css"></head>
<body><a class="skip" href="#main">Skip to content</a><div class="shell"><header class="rail">
<a class="brand" href="/dashboard"><span class="brand-mark" aria-hidden="true">M</span>Montaj</a>
<nav aria-label="Main"><a href="/dashboard" {active}>Production board</a><a href="/">Clip preview <span aria-hidden="true">↗</span></a></nav>
</header><main class="frame" id="main">{content}</main></div></body></html>""".encode()


def _badge(text: str, tone: str) -> str:
    return f'<span class="badge {tone}">{escape(text)}</span>'


def _overview(
    settings: Settings, sources: list[SourceSnapshot], view: str, operations: bool
) -> bytes:
    attention = [source for source in sources if source.tone == "warning"]
    review = [
        source
        for source in sources
        if source.allowed and source.clips[ClipStatus.CHECKED]
    ]
    views = {
        "all": ("All sources", sources),
        "attention": ("Needs attention", attention),
        "review": ("Awaiting review", review),
    }
    view = view if view in views else "all"
    scope, visible = views[view]
    review_count = sum(source.clips[ClipStatus.CHECKED] for source in review)
    filters = "".join(
        f'<a href="/dashboard?view={key}" {"aria-current=page" if key == view else ""}>'
        f"{label} <span>{len(items)}</span></a>"
        for key, (label, items) in views.items()
    )
    try:
        space = f"{disk_free(settings.data_dir) / 1024**3:.1f} GB free"
    except OSError:
        space = "Disk space unavailable"
    rows = "".join(
        f'<tr><th scope="row"><a href="/dashboard/sources/{source.source_id}">{escape(source.title)}</a>'
        f'<span class="secondary">#{source.source_id} · {source.niche}</span></th>'
        f'<td class="status-cell">{_badge(source.state, source.tone)}</td><td class="numeric optional">{source.clips[ClipStatus.CHECKED]}</td>'
        f'<td class="numeric optional">${source.cost:.6f}</td><td class="optional">{source.added}</td></tr>'
        for source in visible
    )
    if visible:
        results = f"""<div class="table-wrap" tabindex="0" role="region" aria-label="Source table"><table>
<caption class="sr-only">{scope}</caption><thead><tr><th scope="col">Source</th><th scope="col">Status</th>
<th scope="col" class="numeric optional">Review clips</th><th scope="col" class="numeric optional">Est. API cost</th><th scope="col" class="optional">Added</th>
</tr></thead><tbody>{rows}</tbody></table></div>"""
    elif sources:
        results = '<div class="empty"><h2>No sources match this view</h2><a href="/dashboard">Show all sources</a></div>'
    else:
        results = '<div class="empty"><h2>No sources yet</h2><p>Import a permitted video with <code>montaj ingest</code>.</p></div>'
    jobs = (
        '<a class="button primary" href="/dashboard/jobs">Production jobs</a>'
        if operations
        else ""
    )
    return render_dashboard_page(
        "Operations",
        f"""<div class="heading"><h1>Production board</h1><div class="toolbar">{jobs}<a class="button" href="/dashboard?view={view}">Refresh</a></div></div>
<div class="board">
<section class="queue" aria-labelledby="sources"><div class="panel-head"><h2 id="sources">Source queue</h2>
<nav class="filters" aria-label="Filter sources">{filters}</nav></div>{results}</section>
<aside class="side" aria-label="Production summary"><section class="panel workload"><div class="section-heading"><h2>Workload</h2></div>
<a class="work-item" href="/dashboard?view=attention"><span><span class="signal warning" aria-hidden="true"></span>Needs attention</span><strong>{len(attention)}</strong></a>
<a class="work-item" href="/dashboard?view=review"><span><span class="signal review" aria-hidden="true"></span>Clips awaiting review</span><strong>{review_count}</strong></a>
</section><section class="panel resources"><div class="section-heading"><h2>Resources</h2></div>
<dl><div><dt>Data volume</dt><dd>{space}</dd></div><div><dt>Est. source API spend</dt><dd>${sum(source.cost for source in sources):.6f}</dd></div></dl></section></aside></div>""",
        'aria-current="page"',
    )


def _detail(source: SourceSnapshot) -> bytes:
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
    review = (
        f'<a class="button primary" href="/?source={source.source_id}">Preview clips <span aria-hidden="true">↗</span></a>'
        if source.allowed
        and any(
            source.clips[state]
            for state in (
                ClipStatus.CAPTIONED,
                ClipStatus.DRAFTED,
                ClipStatus.CHECKED,
                ClipStatus.NEEDS_FIX,
                ClipStatus.APPROVED,
                ClipStatus.EXPORTED,
            )
        )
        else ""
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
<div class="heading"><div><div class="eyebrow">Source #{source.source_id} · {source.niche}</div><h1>{escape(source.title)}</h1></div>
<div class="toolbar">{_badge(source.state, source.tone)}<a class="button" href="/dashboard/sources/{source.source_id}">Refresh</a></div></div>
{notice}<div class="board detail">
<section class="panel"><div class="section-heading"><h2>Saved checkpoints</h2></div><ol class="steps timeline">{checkpoints}</ol></section>
<div class="side"><section class="panel"><div class="section-heading"><h2>Source information</h2></div>
<dl class="facts"><div><dt>Permission</dt><dd>{basis}</dd></div><div><dt>Added</dt><dd>{source.added}</dd></div>
<div><dt>Duration</dt><dd>{source.duration or "Unknown"}</dd></div><div><dt>Est. API cost</dt><dd>${source.cost:.6f}</dd></div></dl></section>
<section class="panel"><div class="section-heading"><h2>Clips</h2>{review}</div>{clip_list}</section></div></div>""",
        'aria-current="location"',
    )


def dashboard_response(
    settings: Settings, path: str, view: str, *, operations: bool = False
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
            return (
                404,
                html,
                render_dashboard_page(
                    "Source not found",
                    '<h1>Source not found</h1><a href="/dashboard">All sources</a>',
                ),
            )
    try:
        sources = _load(settings, source_id)
    except (sqlite3.Error, OSError, ValidationError, ValueError):
        return (
            503,
            html,
            render_dashboard_page(
                "Dashboard unavailable",
                "<h1>Dashboard unavailable</h1><p>Check the data directory and run <code>montaj db init</code> to initialize or migrate the database.</p>",
            ),
        )
    if source_id is None:
        return 200, html, _overview(settings, sources, view, operations)
    if not sources:
        return (
            404,
            html,
            render_dashboard_page(
                "Source not found",
                '<h1>Source not found</h1><a href="/dashboard">All sources</a>',
            ),
        )
    return 200, html, _detail(sources[0])
