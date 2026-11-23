"""Loopback preview of rendered clips and local production operations."""

import html
import shutil
import sqlite3
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Annotated
from urllib.parse import parse_qs, urlencode, urlsplit

import typer
from pydantic import ValidationError

from montaj.config import Settings
from montaj.dashboard import BASE_PATH, dashboard_response, parse_source_id
from montaj.dashboard_jobs import (
    FORM_TYPE,
    JOBS_PATH,
    STAGE_PATH,
    JobQueue,
    StageSubmission,
    Submission,
)
from montaj.db import connect, fetch
from montaj.ingest import source_dir
from montaj.media import MediaError, source_lock
from montaj.models import CheckResult, Clip, ClipStatus, Source
from montaj.rank import clip_words
from montaj.review import review_decision
from montaj.stage_jobs import BoardStage
from montaj.transcribe import Transcript

preview_app = typer.Typer(
    no_args_is_help=True, help="Watch rendered clips on this machine."
)


class PreviewError(ValueError):
    """The preview request is not allowed."""


@dataclass(frozen=True, slots=True)
class PreviewClip:
    clip_id: int
    source_id: int
    transcript: str
    hook_a: str
    hook_b: str
    caption: str
    check_result: str
    basis: str
    media_src: str
    start: float = 0
    end: float = 0
    status: str = ""
    check_detail: str = ""
    version: str = ""


def require_loopback(host: str) -> str:
    """Refuse any bind that is not this machine's IPv4 loopback address."""
    if host != "127.0.0.1":
        raise PreviewError(f"Preview binds to 127.0.0.1 only, not {host}.")
    return host


def resolve_inside(root: Path, relative: str) -> Path:
    """Resolve a data-dir path and reject anything that leaves that directory."""
    requested = Path(relative)
    if requested.is_absolute() or ".." in requested.parts:
        raise PreviewError("That file is outside the data directory.")
    root_resolved = root.resolve()
    candidate = (root_resolved / requested).resolve()
    if not candidate.is_relative_to(root_resolved):
        raise PreviewError("That file is outside the data directory.")
    return candidate


def _text(value: str | None, missing: str) -> str:
    stripped = (value or "").strip()
    return stripped or missing


def _transcript(settings: Settings, clip: Clip) -> str:
    path = source_dir(settings, clip.source_id) / "transcript.json"
    if not path.is_file():
        return "Transcript not found."
    try:
        transcript = Transcript.model_validate_json(path.read_bytes())
    except ValidationError:
        return "Transcript could not be read."
    words = clip_words(transcript, clip.start_s, clip.end_s)
    text = " ".join(word.text for word in words).strip()
    return text or "No words in this clip."


def load_preview_clips(
    settings: Settings, source_id: int | None = None, *, media: str = "serve"
) -> list[PreviewClip]:
    """Clips that have a render and still need a person to look at them."""
    with connect(settings.db_path, read_only=True) as db:
        rows = db.execute(
            """SELECT * FROM clips
               WHERE final_path IS NOT NULL
                 AND status NOT IN ('rejected', 'posted')
                 AND EXISTS (SELECT 1 FROM sources WHERE id = clips.source_id AND source_allowed = 1)
                 AND (? IS NULL OR source_id = ?)
               ORDER BY source_id, id""",
            (source_id, source_id),
        ).fetchall()
        clips = [Clip.model_validate(dict(row)) for row in rows]
        sources = {clip.source_id: fetch(db, Source, clip.source_id) for clip in clips}
    loaded: list[PreviewClip] = []
    for clip in clips:
        if clip.id is None:
            continue
        source = sources.get(clip.source_id)
        loaded.append(
            PreviewClip(
                clip_id=clip.id,
                source_id=clip.source_id,
                transcript=_transcript(settings, clip),
                hook_a=_text(clip.hook_a, "No first hook."),
                hook_b=_text(clip.hook_b, "No second hook."),
                caption=_text(clip.caption, "No caption."),
                check_result=clip.check_result.value
                if clip.check_result
                else "not checked",
                basis=_text(source.allowed_basis if source else None, "not recorded"),
                media_src=f"/media/{clip.id}" if media == "serve" else f"{clip.id}.mp4",
                start=clip.start_s,
                end=clip.end_s,
                status=clip.status.value,
                check_detail=clip.decision_reason or ""
                if clip.check_result in (CheckResult.WARN, CheckResult.FAIL)
                else "",
                version=clip.check_input_hash or "",
            )
        )
    return loaded


def render_page(
    clips: list[PreviewClip],
    *,
    served: bool = False,
    controls: str = "",
    token: str = "",
    notice: str = "",
) -> str:
    """Show the finished clips and optional operations controls."""
    parts = [
        "<!DOCTYPE html>",
        '<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Montaj preview</title>',
        '<link rel="stylesheet" href="/dashboard/style.css">' if served else "",
        '</head><body><main class="frame"><h1>Clips to review</h1>',
    ]
    if served:
        parts.append(
            '<nav aria-label="Main"><a href="/dashboard">Montaj operations</a> · <a href="/">All clips</a></nav>'
        )
    if not clips:
        parts.append("<p>No rendered clips to review.</p>")
    if notice:
        parts.append(f'<p class="notice" role="status">{html.escape(notice)}</p>')
    if controls:
        parts.append(controls)
    for clip in clips:
        parts.append(
            "\n".join(
                (
                    '<article class="panel job-panel clip-panel">',
                    f"<h2>Clip {clip.clip_id}</h2>",
                    f"<p>Source {clip.source_id} · {clip.start:.2f}–{clip.end:.2f} s</p>",
                    f"<p>Permission: {html.escape(clip.basis)}</p>",
                    f"<p>Status: {html.escape(clip.status.replace('_', ' ').capitalize())}</p>",
                    f"<p>Check: {html.escape(clip.check_result)} {html.escape(clip.check_detail)}</p>",
                    f"<p>Hook A: {html.escape(clip.hook_a)}</p>",
                    f"<p>Hook B: {html.escape(clip.hook_b)}</p>",
                    f"<p>Caption: {html.escape(clip.caption)}</p>",
                    f"<p>{html.escape(clip.transcript)}</p>",
                    f'<video controls preload="none" src="{html.escape(clip.media_src, quote=True)}"></video>',
                    _review_form(clip, token) if token else "",
                    "</article>",
                )
            )
        )
    parts.append("</main></body></html>")
    return "\n".join(parts)


def _clip_file(settings: Settings, clip_id: int) -> Path:
    with connect(settings.db_path, read_only=True) as db:
        clip = fetch(db, Clip, clip_id)
        source = fetch(db, Source, clip.source_id) if clip is not None else None
    if (
        clip is None
        or source is None
        or not source.source_allowed
        or not clip.final_path
        or clip.status.value in {"rejected", "posted"}
    ):
        raise PreviewError("That clip is not available to preview.")
    path = resolve_inside(settings.data_dir, clip.final_path)
    if not path.is_file():
        raise PreviewError("The rendered file is missing.")
    return path


def _review_form(clip: PreviewClip, token: str) -> str:
    if clip.status != ClipStatus.CHECKED or not clip.version:
        return ""
    fields = (
        f'<input type="hidden" name="token" value="{token}">'
        f'<input type="hidden" name="source" value="{clip.source_id}">'
        f'<input type="hidden" name="clip" value="{clip.clip_id}">'
        f'<input type="hidden" name="version" value="{html.escape(clip.version, quote=True)}">'
    )
    confirmation = (
        '<label class="inline-field"><input type="checkbox" name="confirm" value="yes" required> I accept this warning</label>'
        if clip.check_result == CheckResult.WARN
        else ""
    )
    return (
        f'<form class="job-form" action="/dashboard/review" method="post">{fields}{confirmation}'
        '<button class="button primary" name="action" value="approve">Approve</button></form>'
        f'<form class="job-form" action="/dashboard/review" method="post">{fields}'
        '<label>Rejection reason <select name="reason"><option>content</option><option>context</option>'
        "<option>hook</option><option>framing</option><option>captions</option>"
        "<option>campaign</option></select></label>"
        '<button class="button" name="action" value="reject">Reject</button></form>'
    )


def _export_accounts(settings: Settings, source_id: int) -> dict[int, str]:
    with connect(settings.db_path, read_only=True) as db:
        rows = db.execute(
            """SELECT accounts.id, accounts.handle FROM accounts
               JOIN sources ON accounts.niche = sources.niche
               JOIN campaigns ON campaigns.id = sources.campaign_id
                 AND campaigns.platform = accounts.platform
               WHERE sources.id = ? AND sources.source_allowed = 1
               ORDER BY accounts.handle""",
            (source_id,),
        )
        return {row[0]: row[1] for row in rows}


def _export_form(settings: Settings, source_id: int, token: str) -> str:
    accounts = _export_accounts(settings, source_id)
    if not accounts:
        return "<p>Set up a posting account for this campaign to export clips.</p>"
    options = "".join(
        f'<option value="{key}">{html.escape(handle)}</option>'
        for key, handle in accounts.items()
    )
    return (
        '<form class="job-form panel job-panel" action="/dashboard/export" method="post">'
        f'<input type="hidden" name="token" value="{token}">'
        f'<input type="hidden" name="source" value="{source_id}">'
        f'<label>Posting account <select name="account">{options}</select></label>'
        '<button class="button">Export approved clips</button></form>'
    )


def handle_preview(
    settings: Settings, target: str, jobs: JobQueue | None = None
) -> tuple[int, str, bytes]:
    """Answer one read-only GET. Paths that leave the data directory are rejected."""
    request = urlsplit(target)
    path = request.path
    query = parse_qs(request.query, keep_blank_values=True)
    content_type = "text/html; charset=utf-8"
    if jobs is not None and path == JOBS_PATH:
        return 200, content_type, jobs.page(query.get("notice", [""])[0])
    if path == BASE_PATH or path.startswith(f"{BASE_PATH}/"):
        return dashboard_response(
            settings, path, query.get("view", [""])[0], operations=jobs is not None
        )
    if path in {"", "/"}:
        source_id = parse_source_id(query["source"][0]) if "source" in query else None
        if "source" in query and source_id is None:
            return 404, "text/plain; charset=utf-8", b"Source not found."
        controls = ""
        token = ""
        if jobs is not None and source_id is not None:
            token = jobs.token
            controls = _export_form(settings, source_id, token)
        clips = load_preview_clips(settings, source_id)
        if query.get("ready") == ["1"]:
            clips = [
                clip
                for clip in clips
                if clip.status
                in (ClipStatus.CHECKED, ClipStatus.APPROVED, ClipStatus.EXPORTED)
                and clip.check_result in (CheckResult.PASS, CheckResult.WARN)
            ]
        page = render_page(
            clips,
            served=True,
            controls=controls,
            token=token,
            notice=query.get("notice", [""])[0],
        )
        return 200, content_type, page.encode("utf-8")
    media = "/media/"
    clip_id = (
        parse_source_id(path.removeprefix(media)) if path.startswith(media) else None
    )
    if clip_id is not None:
        try:
            file_path = _clip_file(settings, clip_id)
        except PreviewError as exc:
            return 404, "text/plain; charset=utf-8", str(exc).encode("utf-8")
        return 200, "video/mp4", file_path.read_bytes()
    return 404, "text/plain; charset=utf-8", b"Not found."


def export_preview(settings: Settings, source_id: int, dest: Path) -> int:
    """Copy one source's rendered clips and a matching index into one folder."""
    clips = load_preview_clips(settings, source_id, media="file")
    dest.mkdir(parents=True, exist_ok=True)
    for clip in clips:
        with connect(settings.db_path) as db:
            stored = fetch(db, Clip, clip.clip_id)
        if stored is None or not stored.final_path:
            raise PreviewError(f"Clip {clip.clip_id} has no rendered file.")
        source = resolve_inside(settings.data_dir, stored.final_path)
        if not source.is_file():
            raise PreviewError(f"Clip {clip.clip_id} render is missing.")
        shutil.copyfile(source, dest / f"{clip.clip_id}.mp4")
    (dest / "index.html").write_text(render_page(clips), encoding="utf-8")
    return len(clips)


def _stage_submission(source: int, form: dict[str, str]) -> StageSubmission:
    try:
        stage = BoardStage(form.get("stage", ""))
    except ValueError:
        raise ValueError("Choose a valid stage.") from None
    fields = {key: form[key] for key in ("top", "budget") if form.get(key)}
    try:
        return StageSubmission.model_validate(
            {
                "source": source,
                "stage": stage,
                "confirm": form.get("confirm") == "yes",
                "retry": form.get("retry") == "yes",
                **fields,
            }
        )
    except ValidationError:
        raise ValueError("Check the clip target and budget, then try again.") from None


def _post_action(
    settings: Settings, jobs: JobQueue, path: str, form: dict[str, str]
) -> str:
    if path == JOBS_PATH:
        jobs.submit(
            Submission.model_validate(
                {key: value for key, value in form.items() if value}
            )
        )
        return JOBS_PATH
    source = parse_source_id(form.get("source", ""))
    if source is None:
        raise ValueError("A valid source ID is required.")
    if path == STAGE_PATH:
        jobs.submit_stage(_stage_submission(source, form))
        return f"{BASE_PATH}/sources/{source}"
    if path == "/dashboard/review":
        clip = parse_source_id(form.get("clip", ""))
        if clip is None:
            raise ValueError("A valid clip ID is required.")
        if form.get("action") == "approve":
            _clip_file(settings, clip)
        message = review_decision(
            settings,
            source,
            clip,
            action=form.get("action", ""),
            version=form.get("version", ""),
            confirm_warning=form.get("confirm") == "yes",
            reason=form.get("reason", ""),
        )
    elif path == "/dashboard/export":
        from montaj.export import export_account

        account = parse_source_id(form.get("account", ""))
        if account is None or account not in _export_accounts(settings, source):
            raise ValueError("Choose a posting account for this source's campaign.")
        with source_lock(source_dir(settings, source), command="export"):
            messages: list[str] = []
            exported, refused, folder = export_account(
                settings, account, source_id=source, writer=messages.append
            )
        message = " ".join(
            [
                f"Exported {exported} clips to {folder}." if folder is not None else "",
                f"{refused} clips were refused." if refused else "",
                *messages,
            ]
        ).strip()
    else:
        raise ValueError("Unknown dashboard operation.")
    return "/?" + urlencode({"source": source, "ready": 1, "notice": message})


def _handler(
    settings: Settings, jobs: JobQueue | None = None
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _local_request(self) -> bool:
            hostname, separator, port = self.headers.get("Host", "").partition(":")
            if hostname.lower() not in {"127.0.0.1", "localhost"} or (
                separator and (not port.isascii() or not port.isdecimal())
            ):
                self.send_error(400, "Use a localhost address")
                return False
            return True

        def do_GET(self) -> None:
            if not self._local_request():
                return
            try:
                status, content_type, body = handle_preview(settings, self.path, jobs)
            except (OSError, sqlite3.Error, ValueError):
                self.send_error(503, "Preview unavailable")
                return
            self._respond(status, content_type, body)

        def _respond(self, status: int, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; style-src 'self'; media-src 'self'; "
                "form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
            )
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            if not self._local_request():
                return
            if jobs is None or self.path not in {
                JOBS_PATH,
                STAGE_PATH,
                "/dashboard/review",
                "/dashboard/export",
            }:
                self.send_error(405)
                return
            if not _same_origin(
                self.headers.get("Host", ""), self.headers.get("Origin", "")
            ):
                self.send_error(403)
                return
            if self.headers.get_content_type() != FORM_TYPE:
                self.send_error(415)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 8192:
                    self.send_error(413)
                    return
                self.connection.settimeout(10)
                form = {
                    key: values[-1]
                    for key, values in parse_qs(
                        self.rfile.read(length).decode("utf-8"),
                        keep_blank_values=True,
                        max_num_fields=16,
                    ).items()
                }
                if not jobs.accepts_token(form.get("token", "")):
                    self.send_error(403)
                    return
                location = _post_action(settings, jobs, self.path, form)
            except (
                MediaError,
                OSError,
                sqlite3.Error,
                ValueError,
                RuntimeError,
            ) as exc:
                if self.path == JOBS_PATH:
                    self._respond(400, "text/html; charset=utf-8", jobs.page(str(exc)))
                else:
                    self._respond(
                        400,
                        "text/html; charset=utf-8",
                        f'<p>{html.escape(str(exc))}</p><p><a href="/dashboard">Return to dashboard</a></p>'.encode(),
                    )
                return
            self.send_response(303)
            self.send_header("Location", location)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def _same_origin(host: str, origin: str) -> bool:
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    return (
        parsed.scheme == "http"
        and parsed.netloc == host
        and not (parsed.path or parsed.query or parsed.fragment)
    )


def serve_preview(
    settings: Settings,
    port: int = 8765,
    host: str = "127.0.0.1",
    *,
    operations: bool = False,
) -> None:
    """Listen until interrupted. The host must be loopback."""
    bound = require_loopback(host)
    jobs = JobQueue(settings) if operations else None
    server = ThreadingHTTPServer((bound, port), _handler(settings, jobs))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return
    finally:
        server.server_close()


@preview_app.command("serve")
def serve(
    port: Annotated[int, typer.Option(min=1, max=65535)] = 8765,
) -> None:
    """Serve rendered clips on 127.0.0.1. Reach a remote host through an SSH tunnel."""
    _start_server(port, "Preview", "/")


def dashboard(port: Annotated[int, typer.Option(min=1, max=65535)] = 8765) -> None:
    """Operate production and review on 127.0.0.1."""
    _start_server(port, "Dashboard", BASE_PATH)


def _start_server(port: int, name: str, path: str) -> None:
    settings = Settings.from_env()
    if not settings.db_path.is_file():
        typer.echo("Database is missing. Run montaj db init first.", err=True)
        raise typer.Exit(1)
    typer.echo(f"{name} at http://127.0.0.1:{port}{path}  (Ctrl+C to stop)")
    try:
        if path == BASE_PATH:
            serve_preview(settings, port, operations=True)
        else:
            serve_preview(settings, port)
    except OSError as exc:
        typer.echo(f"{name} failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"{name} stopped.")


@preview_app.command("export")
def export_command(
    source_id: Annotated[int, typer.Argument(min=1)],
    dest: Annotated[
        Path | None,
        typer.Option(help="Folder to write. Defaults to data/preview/<source id>."),
    ] = None,
) -> None:
    """Copy rendered clips and an index page into one folder."""
    settings = Settings.from_env()
    target = dest or (settings.data_dir / "preview" / str(source_id))
    try:
        count = export_preview(settings, source_id, target)
    except (PreviewError, OSError) as exc:
        typer.echo(f"Preview export failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"Wrote {count} clip(s) to {target}")
