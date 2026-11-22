from pathlib import Path

import pytest
from typer.testing import CliRunner

from montaj.cli import app
from montaj.config import Settings
from montaj.db import connect, insert
from montaj.models import CheckResult, Clip, ClipStatus, Niche, Source
from montaj.preview import (
    PreviewError,
    export_preview,
    handle_preview,
    require_loopback,
    resolve_inside,
    serve_preview,
)
from montaj.transcribe import Transcript, Word


def _settings(db_path: Path) -> Settings:
    return Settings(data_dir=db_path.parent)


def _clip(settings: Settings, *, caption: str, status: ClipStatus, final: str) -> int:
    with connect(settings.db_path) as db:
        source = insert(
            db,
            Source(
                path="video.mp4",
                title="Fixture",
                niche=Niche.GAMES,
                source_allowed=True,
                allowed_basis="creator written permission",
            ),
        )
        assert source.id is not None
        directory = settings.data_dir / "sources" / str(source.id)
        directory.mkdir(parents=True)
        transcript = Transcript(
            text="Hello there",
            language="en",
            words=[
                Word(text="Hello", start=0.1, end=0.4),
                Word(text="there", start=0.5, end=0.8),
            ],
        )
        (directory / "transcript.json").write_text(
            transcript.model_dump_json(), encoding="utf-8"
        )
        clip = insert(
            db,
            Clip(
                source_id=source.id,
                start_s=0,
                end_s=2,
                status=status,
                hook_a="<script>alert(1)</script>",
                hook_b="Second hook",
                caption=caption,
                final_path=final,
                check_result=CheckResult.PASS,
            ),
        )
    assert clip.id is not None
    return clip.id


def test_paths_outside_the_data_dir_are_rejected(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    inside = data / "clips" / "1.mp4"
    inside.parent.mkdir()
    inside.write_bytes(b"video")
    assert resolve_inside(data, "clips/1.mp4") == inside.resolve()
    with pytest.raises(PreviewError, match="outside"):
        resolve_inside(data, "../secret.mp4")
    with pytest.raises(PreviewError, match="outside"):
        resolve_inside(data, str(tmp_path / "secret.mp4"))


def test_preview_lists_a_rendered_clip_and_serves_its_file(db_path: Path) -> None:
    settings = _settings(db_path)
    video = settings.data_dir / "clips" / "kept" / "final.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"fake-video")
    kept = _clip(
        settings,
        caption="A safe caption",
        status=ClipStatus.CHECKED,
        final="clips/kept/final.mp4",
    )
    hidden = _clip(
        settings,
        caption="posted-secret",
        status=ClipStatus.POSTED,
        final="clips/kept/final.mp4",
    )
    status, content_type, body = handle_preview(settings, "/")
    page = body.decode()
    assert status == 200
    assert "text/html" in content_type
    assert "Hello there" in page
    assert "Second hook" in page
    assert "A safe caption" in page
    assert "Check: pass" in page
    assert "creator written permission" in page
    assert "<script>" not in page
    assert "posted-secret" not in page
    assert f"/media/{kept}" in page
    assert f"/media/{hidden}" not in page
    media_status, media_type, media = handle_preview(settings, f"/media/{kept}")
    assert (media_status, media_type, media) == (200, "video/mp4", b"fake-video")
    assert handle_preview(settings, "/media/../secret")[0] == 404


def test_preview_export_copies_the_clip_and_refuses_an_escape(db_path: Path) -> None:
    settings = _settings(db_path)
    video = settings.data_dir / "clips" / "1" / "final.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"fake-video")
    clip_id = _clip(
        settings,
        caption="A safe caption",
        status=ClipStatus.CHECKED,
        final="clips/1/final.mp4",
    )
    dest = settings.data_dir / "out"
    assert export_preview(settings, 1, dest) == 1
    assert (dest / f"{clip_id}.mp4").read_bytes() == b"fake-video"
    assert "Hello there" in (dest / "index.html").read_text(encoding="utf-8")

    with connect(settings.db_path) as db:
        db.execute(
            "UPDATE clips SET final_path = ? WHERE id = ?", ("../secret.mp4", clip_id)
        )
    secret = settings.data_dir.parent / "secret.mp4"
    secret.write_bytes(b"do-not-copy")
    with pytest.raises(PreviewError, match="outside"):
        export_preview(settings, 1, dest / "escaped")
    assert not any(
        path.read_bytes() == b"do-not-copy" for path in (dest / "escaped").glob("*")
    )


def test_preview_refuses_a_public_bind(db_path: Path) -> None:
    with pytest.raises(PreviewError, match="127.0.0.1"):
        require_loopback("0.0.0.0")
    with pytest.raises(PreviewError, match="127.0.0.1"):
        serve_preview(_settings(db_path), host="0.0.0.0")
    assert require_loopback("127.0.0.1") == "127.0.0.1"


def test_preview_serve_command_stays_on_loopback(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    def fake(settings: Settings, port: int = 8765, host: str = "127.0.0.1") -> None:
        seen["host"] = host
        seen["port"] = port

    monkeypatch.setattr("montaj.preview.serve_preview", fake)
    result = CliRunner().invoke(app, ["preview", "serve", "--port", "8765"])
    assert result.exit_code == 0, result.output
    assert seen == {"host": "127.0.0.1", "port": 8765}
    assert "http://127.0.0.1:8765/" in result.output
