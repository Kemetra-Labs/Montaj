"""Read-only local preview of rendered clips. Nothing here approves or posts."""

import html
import shutil
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

import typer
from pydantic import ValidationError

from montaj.config import Settings
from montaj.db import connect, fetch
from montaj.ingest import source_dir
from montaj.models import Clip, Source
from montaj.rank import clip_words
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
    with connect(settings.db_path) as db:
        rows = db.execute(
            """SELECT * FROM clips
               WHERE final_path IS NOT NULL
                 AND status NOT IN ('rejected', 'posted')
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
            )
        )
    return loaded


def render_page(clips: list[PreviewClip]) -> str:
    """One read-only page. Approval stays in the terminal."""
    parts = [
        "<!DOCTYPE html>",
        '<html lang="en"><head><meta charset="utf-8"><title>Montaj preview</title></head>',
        "<body><h1>Clips to review</h1>",
    ]
    if not clips:
        parts.append("<p>No rendered clips to review.</p>")
    for clip in clips:
        parts.append(
            "\n".join(
                (
                    "<article>",
                    f"<h2>Clip {clip.clip_id}</h2>",
                    f"<p>Source {clip.source_id}</p>",
                    f"<p>Permission: {html.escape(clip.basis)}</p>",
                    f"<p>Check: {html.escape(clip.check_result)}</p>",
                    f"<p>Hook A: {html.escape(clip.hook_a)}</p>",
                    f"<p>Hook B: {html.escape(clip.hook_b)}</p>",
                    f"<p>Caption: {html.escape(clip.caption)}</p>",
                    f"<p>{html.escape(clip.transcript)}</p>",
                    f'<video controls src="{html.escape(clip.media_src, quote=True)}"></video>',
                    "</article>",
                )
            )
        )
    parts.append("</body></html>")
    return "\n".join(parts)


def _clip_file(settings: Settings, clip_id: int) -> Path:
    with connect(settings.db_path) as db:
        clip = fetch(db, Clip, clip_id)
    if (
        clip is None
        or not clip.final_path
        or clip.status.value in {"rejected", "posted"}
    ):
        raise PreviewError("That clip is not available to preview.")
    path = resolve_inside(settings.data_dir, clip.final_path)
    if not path.is_file():
        raise PreviewError("The rendered file is missing.")
    return path


def handle_preview(settings: Settings, target: str) -> tuple[int, str, bytes]:
    """Answer one read-only GET. Paths that leave the data directory are rejected."""
    path = urlsplit(target).path
    if path in {"", "/"}:
        page = render_page(load_preview_clips(settings)).encode("utf-8")
        return 200, "text/html; charset=utf-8", page
    media = "/media/"
    if path.startswith(media) and path.removeprefix(media).isdigit():
        try:
            file_path = _clip_file(settings, int(path.removeprefix(media)))
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


def _handler(settings: Settings) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            status, content_type, body = handle_preview(settings, self.path)
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            self.send_error(405)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def serve_preview(
    settings: Settings, port: int = 8765, host: str = "127.0.0.1"
) -> None:
    """Listen until interrupted. The host must be loopback."""
    bound = require_loopback(host)
    server = ThreadingHTTPServer((bound, port), _handler(settings))
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
    settings = Settings.from_env()
    if not settings.db_path.is_file():
        typer.echo("Database is missing. Run montaj db init first.", err=True)
        raise typer.Exit(1)
    typer.echo(f"Preview at http://127.0.0.1:{port}/  (Ctrl+C to stop)")
    try:
        serve_preview(settings, port)
    except OSError as exc:
        typer.echo(f"Preview failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo("Preview stopped.")


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
