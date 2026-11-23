from collections.abc import Callable
from email.message import Message
from io import BytesIO
from pathlib import Path
from threading import Thread
from unittest.mock import Mock
from urllib.parse import urlencode

import pytest
from typer.testing import CliRunner

from montaj.budget import Budget
from montaj.cli import app
from montaj.config import Settings
from montaj.costs import Service, Usage, log_api_call
from montaj.dashboard_jobs import JOBS_PATH, JobQueue, Submission, dashboard_location
from montaj.db import connect, insert
from montaj.ingest import SourceMetadata
from montaj.job import JobResult
from montaj.models import CheckResult, Clip, ClipStatus, Niche, Source
from montaj.preview import _handler, handle_preview


def test_overview_uses_saved_sources_and_filters_attention(db_path: Path) -> None:
    settings = Settings(data_dir=db_path.parent)
    with connect(db_path) as db:
        insert(
            db,
            Source(
                id=1,
                path="input.mp4",
                title="<Interview>",
                niche=Niche.HEALTH,
                source_allowed=True,
            ),
        )
        insert(
            db,
            Source(
                id=2,
                url="https://example.test/?key=private-value",
                title="https://example.test/?key=private-value",
                niche=Niche.GAMES,
                source_allowed=True,
                speech_status="failed",
                failure_note="YouTube requires a supported JavaScript runtime. Cookie: private-cookie",
            ),
        )
    status, content_type, body = handle_preview(settings, "/dashboard")
    assert status == 200 and "text/html" in content_type
    assert b'href="/dashboard/style.css"' in body
    css_status, css_type, css = handle_preview(settings, "/dashboard/style.css")
    assert css_status == 200 and "text/css" in css_type and b".table-wrap" in css
    assert b"&lt;Interview&gt;" in body
    assert b"/dashboard/sources/1" in body
    assert b"Needs attention" in body
    assert b"private-value" not in body
    assert b"private-cookie" not in body
    attention = handle_preview(settings, "/dashboard?view=attention")[2]
    assert b"/dashboard/sources/2" in attention
    assert b"/dashboard/sources/1" not in attention
    detail = handle_preview(settings, "/dashboard/sources/2")[2]
    assert b"Install Deno" in detail
    assert b"private-cookie" not in detail


def test_source_detail_shows_checkpoints_clip_status_and_cost(db_path: Path) -> None:
    settings = Settings(data_dir=db_path.parent)
    with connect(db_path) as db:
        insert(
            db,
            Source(
                id=1,
                path="input.mp4",
                title="Interview",
                niche=Niche.HEALTH,
                source_allowed=True,
                allowed_basis="Creator approval",
            ),
        )
        insert(
            db,
            Clip(
                id=1,
                source_id=1,
                start_s=0,
                end_s=5,
                status=ClipStatus.CHECKED,
                check_result=CheckResult.PASS,
                final_path="clips/1/final.mp4",
            ),
        )
        log_api_call(db, 1, "transcribe", Service.DEEPGRAM, Usage(seconds=12), 0.012345)
    folder = settings.data_dir / "sources/1"
    folder.mkdir(parents=True)
    (folder / "meta.json").write_text(
        SourceMetadata(
            title="Interview", duration=72, video_hash="video", audio_hash="audio"
        ).model_dump_json(),
        encoding="utf-8",
    )
    for name in ("video.mp4", "audio.wav", "transcript.json"):
        (folder / name).write_bytes(b"fixture")
    status, _, body = handle_preview(settings, "/dashboard/sources/1")
    assert status == 200
    for excerpt in (
        b"Creator approval",
        b"Imported",
        b"Transcript",
        b"Saved",
        b"$0.012345",
        b"Awaiting review",
        b"/?source=1",
    ):
        assert excerpt in body
    (folder / "video.mp4").unlink()
    assert b"Media missing" in handle_preview(settings, "/dashboard/sources/1")[2]
    assert handle_preview(settings, "/dashboard/sources/99")[0] == 404
    assert handle_preview(settings, "/dashboard/sources/9223372036854775808")[0] == 404


def test_dashboard_empty_and_missing_database(db_path: Path, tmp_path: Path) -> None:
    empty = handle_preview(Settings(data_dir=db_path.parent), "/dashboard")
    assert empty[0] == 200 and b"No sources yet" in empty[2]
    missing = Settings(data_dir=tmp_path / "missing")
    assert handle_preview(missing, "/dashboard")[0] == 503
    assert not missing.db_path.exists()


def test_dashboard_command_uses_shared_localhost_server(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[int, bool]] = []
    monkeypatch.setattr(
        "montaj.preview.serve_preview",
        lambda settings, port, *, operations: seen.append((port, operations)),
    )
    result = CliRunner().invoke(app, ["dashboard", "--port", "8766"])
    assert result.exit_code == 0, result.output
    assert seen == [(8766, True)]
    assert "http://127.0.0.1:8766/dashboard" in result.output


def test_dashboard_jobs_only_accept_youtube_or_an_inbox_file(db_path: Path) -> None:
    settings = Settings(data_dir=db_path.parent)
    inbox = settings.data_dir / "inbox"
    inbox.mkdir()
    video = inbox / "video.mp4"
    video.touch()
    outside = settings.data_dir / "other.mp4"
    outside.touch()
    assert dashboard_location(settings, "video.mp4") == str(video.resolve())
    assert (
        dashboard_location(settings, "https://youtu.be/video_id001")
        == "https://youtu.be/video_id001"
    )
    for location in (
        "../other.mp4",
        "https://localhost/video",
        "https://youtube.com:444/v",
        "https://youtube.com/redirect?q=https://example.com",
        r"\\server\share\video.mp4",
        r"C:video.mp4",
    ):
        with pytest.raises(ValueError):
            dashboard_location(settings, location)


def test_dashboard_job_worker_exposes_its_batch_result(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(data_dir=db_path.parent)
    jobs = JobQueue(settings)
    threads: list[Thread] = []

    def worker(
        *, target: Callable[..., None], args: tuple[object, ...], daemon: bool
    ) -> Thread:
        thread = Thread(target=target, args=args, daemon=daemon)
        threads.append(thread)
        return thread

    def fake_batch(
        settings: Settings,
        source: int,
        *,
        top: int,
        budget: Budget,
        progress: Callable[[str], None],
    ) -> JobResult:
        assert source == 7 and top == 2
        progress("Checking clips")
        return JobResult(source, top, ready=[3, 4])

    monkeypatch.setattr("montaj.dashboard_jobs.Thread", worker)
    monkeypatch.setattr("montaj.dashboard_jobs.produce_batch", fake_batch)
    jobs.submit(Submission(resume=7, top=2))
    threads[0].join(timeout=5)
    assert not threads[0].is_alive()
    page = jobs.page()
    assert b"2/2 clips ready" in page and b"/?source=7&amp;ready=1" in page


@pytest.mark.parametrize(
    ("operations", "origin", "valid_token", "status"),
    [
        (False, "http://127.0.0.1:8765", True, 405),
        (True, "https://other.example", True, 403),
        (True, "http://127.0.0.1:8765/path", True, 403),
        (True, "http://127.0.0.1:8765", False, 403),
        (True, "http://127.0.0.1:8765", True, 303),
    ],
)
def test_job_post_requires_the_operations_server_and_same_origin_token(
    db_path: Path,
    operations: bool,
    origin: str,
    valid_token: bool,
    status: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(data_dir=db_path.parent)
    jobs = JobQueue(settings)
    submit = Mock()
    monkeypatch.setattr(jobs, "submit", submit)
    handler = _handler(settings, jobs if operations else None)
    request = object.__new__(handler)
    request.headers = Message()
    request.headers["Host"] = "127.0.0.1:8765"
    request.headers["Origin"] = origin
    request.headers["Content-Type"] = "application/x-www-form-urlencoded"
    body = urlencode(
        {"token": jobs.token if valid_token else "wrong", "resume": 1}
    ).encode()
    request.headers["Content-Length"] = str(len(body))
    request.path = JOBS_PATH
    request.rfile = BytesIO(body)
    request.connection = Mock()
    request.send_error = Mock()
    request.send_response = Mock()
    request.send_header = Mock()
    request.end_headers = Mock()
    request.do_POST()
    if status == 303:
        request.send_response.assert_called_once_with(303)
        submit.assert_called_once()
        assert submit.call_args.args[0].resume == 1
    else:
        request.send_error.assert_called_once_with(status)
        submit.assert_not_called()
