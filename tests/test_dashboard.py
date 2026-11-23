from email.message import Message
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import urlencode

import pytest
from typer.testing import CliRunner

from montaj.cli import app
from montaj.config import Settings
from montaj.costs import Service, Usage, log_api_call
from montaj.dashboard_jobs import (
    JOBS_PATH,
    JobQueue,
    StageSubmission,
    dashboard_location,
)
from montaj.db import connect, insert
from montaj.ingest import SourceMetadata
from montaj.models import CheckResult, Clip, ClipStatus, Niche, Source
from montaj.preview import _handler, handle_preview
from montaj.stage_jobs import BoardStage
from tests.dashboard_helpers import (
    _add_source,
    _capture_threads,
    _post_request,
)


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
def test_stage_post_requires_token_and_same_origin(
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
    monkeypatch.setattr(jobs, "submit_stage", submit)
    request = _post_request(
        settings,
        jobs if operations else None,
        {"source": 7, "stage": "render"},
        origin=origin,
        token=None if valid_token else "wrong",
    )
    if status == 303:
        request.send_response.assert_called_once_with(303)
        request.send_header.assert_any_call("Location", "/dashboard/sources/7")
        submit.assert_called_once()
        assert submit.call_args.args[0] == StageSubmission(
            source=7, stage=BoardStage.RENDER
        )
    else:
        request.send_error.assert_called_once_with(status)
        submit.assert_not_called()


def test_stage_post_maps_the_form_to_a_submission(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(data_dir=db_path.parent)
    jobs = JobQueue(settings)
    submit = Mock()
    monkeypatch.setattr(jobs, "submit_stage", submit)
    request = _post_request(
        settings,
        jobs,
        {
            "source": 7,
            "stage": "rank",
            "top": 4,
            "budget": "2.5",
            "confirm": "yes",
            "retry": "yes",
        },
    )
    request.send_response.assert_called_once_with(303)
    assert submit.call_args.args[0] == StageSubmission(
        source=7,
        stage=BoardStage.RANK,
        top=4,
        confirm=True,
        retry=True,
    )


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"source": 7, "stage": "bogus"}, "Choose a valid stage."),
        ({"source": 7}, "Choose a valid stage."),
        ({"stage": "render"}, "A valid source ID is required."),
        ({"source": "abc", "stage": "render"}, "A valid source ID is required."),
        ({"source": 7, "stage": "render", "top": 0}, "clip target"),
    ],
)
def test_stage_post_rejects_unknown_stage_and_missing_source(
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fields: dict[str, object],
    message: str,
) -> None:
    settings = Settings(data_dir=db_path.parent)
    jobs = JobQueue(settings)
    submit = Mock()
    monkeypatch.setattr(jobs, "submit_stage", submit)
    request = _post_request(settings, jobs, fields)
    request.send_response.assert_called_once_with(400)
    body = request.wfile.getvalue().decode()
    assert message in body and "Traceback" not in body and "pydantic" not in body
    submit.assert_not_called()


def test_stage_post_shows_a_refused_job_as_a_message(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(data_dir=db_path.parent)
    jobs = JobQueue(settings)
    refusal = ValueError("Confirm the estimated cost first.")
    monkeypatch.setattr(jobs, "submit_stage", Mock(side_effect=refusal))
    request = _post_request(settings, jobs, {"source": 7, "stage": "rank"})
    request.send_response.assert_called_once_with(400)
    assert b"Confirm the estimated cost first." in request.wfile.getvalue()


def test_unconfirmed_paid_stage_post_is_refused_and_starts_nothing(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_source(db_path)
    settings = Settings(data_dir=db_path.parent)
    jobs = JobQueue(settings)
    threads = _capture_threads(monkeypatch)
    runner = Mock()
    monkeypatch.setattr("montaj.dashboard_jobs.run_board_stage", runner)
    request = _post_request(settings, jobs, {"source": 7, "stage": "draft"})
    request.send_error.assert_not_called()
    request.send_response.assert_called_once_with(400)
    assert b"Confirm the estimated cost first." in request.wfile.getvalue()
    assert threads == [] and jobs.snapshot() is None
    runner.assert_not_called()


def _get_request(
    settings: Settings, jobs: JobQueue | None, path: str, fetch_site: str | None
) -> Mock:
    request = object.__new__(_handler(settings, jobs))
    request.headers = Message()
    request.headers["Host"] = "127.0.0.1:8765"
    if fetch_site is not None:
        request.headers["Sec-Fetch-Site"] = fetch_site
    request.path = path
    request.wfile = BytesIO()
    request.send_error = Mock()
    request.send_response = Mock()
    request.send_header = Mock()
    request.end_headers = Mock()
    request.do_GET()
    return request  # type: ignore[return-value]


@pytest.mark.parametrize(
    ("path", "fetch_site", "refused"),
    [
        ("/dashboard", "cross-site", True),
        ("/dashboard/sources/7/confirm?stage=batch", "cross-site", True),
        ("/dashboard", "same-origin", False),
        ("/dashboard", "none", False),
        ("/dashboard", None, False),
        ("/", "cross-site", False),
    ],
)
def test_cross_site_dashboard_gets_are_refused(
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    fetch_site: str | None,
    refused: bool,
) -> None:
    _add_source(db_path)
    settings = Settings(data_dir=db_path.parent)
    jobs = JobQueue(settings)
    estimate = Mock(return_value=["line"])
    monkeypatch.setattr(jobs, "estimate_batch", estimate)
    request = _get_request(settings, jobs, path, fetch_site)
    if refused:
        request.send_error.assert_called_once_with(403)
        request.send_response.assert_not_called()
        estimate.assert_not_called()
    else:
        request.send_error.assert_not_called()
        request.send_response.assert_called_once_with(200)
