from collections.abc import Callable
from email.message import Message
from io import BytesIO
from pathlib import Path
from threading import Event, Thread
from unittest.mock import Mock
from urllib.parse import urlencode

import pytest
from typer.testing import CliRunner

from montaj.budget import Budget
from montaj.cli import app
from montaj.config import Settings
from montaj.costs import Service, Usage, log_api_call
from montaj.dashboard import LiveJob, dashboard_response
from montaj.dashboard_jobs import (
    JOBS_PATH,
    JobQueue,
    StageSubmission,
    Submission,
    dashboard_location,
)
from montaj.db import connect, insert
from montaj.ingest import SourceMetadata
from montaj.job import JobResult
from montaj.media import MediaError
from montaj.models import CheckResult, Clip, ClipStatus, Niche, Source
from montaj.preview import _handler, handle_preview
from montaj.stage_jobs import PAID_STAGES, BoardStage, StageResult


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


def test_dashboard_job_progress_is_per_server_and_resume_uses_source_id(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(data_dir=db_path.parent)
    jobs = JobQueue(settings)
    threads: list[Thread] = []
    started = Event()
    finish = Event()

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
        started.set()
        assert finish.wait(timeout=5)
        return JobResult(source, top, ready=[3, 4])

    monkeypatch.setattr("montaj.dashboard_jobs.Thread", worker)
    monkeypatch.setattr("montaj.dashboard_jobs.produce_batch", fake_batch)
    jobs.submit(Submission(resume=7, top=2))
    try:
        assert started.wait(timeout=5)
        with pytest.raises(ValueError, match="already running"):
            jobs.submit(Submission(resume=8, top=2))
        activity = jobs.snapshot()
        assert activity is not None and activity.stage == "Checking clips"
        restarted = JobQueue(settings)
        assert restarted.snapshot() is None
    finally:
        finish.set()
        threads[0].join(timeout=5)
    assert not threads[0].is_alive()
    page = handle_preview(settings, "/dashboard", jobs)[2]
    assert b"2/2 clips ready" in page and b"/?source=7&amp;ready=1" in page
    restarted.submit(Submission(resume=7, top=2))
    threads[1].join(timeout=5)
    assert not threads[1].is_alive()
    assert b"2/2 clips ready" in handle_preview(settings, "/dashboard", restarted)[2]


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


STAGE_PATH = "/dashboard/stage"


def _add_source(
    db_path: Path, *, allowed: bool = True, basis: str = "Creator approval"
) -> None:
    with connect(db_path) as db:
        insert(
            db,
            Source(
                id=7,
                path="input.mp4",
                title="Interview",
                niche=Niche.HEALTH,
                source_allowed=allowed,
                allowed_basis=basis,
            ),
        )


def _capture_threads(monkeypatch: pytest.MonkeyPatch) -> list[Thread]:
    threads: list[Thread] = []

    def worker(
        *, target: Callable[..., None], args: tuple[object, ...], daemon: bool
    ) -> Thread:
        thread = Thread(target=target, args=args, daemon=daemon)
        threads.append(thread)
        return thread

    monkeypatch.setattr("montaj.dashboard_jobs.Thread", worker)
    return threads


def test_stage_submit_runs_one_stage_and_blocks_a_second_job(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_source(db_path)
    jobs = JobQueue(Settings(data_dir=db_path.parent))
    threads = _capture_threads(monkeypatch)
    started, finish = Event(), Event()

    def fake_stage(
        settings: Settings,
        source: int,
        stage: BoardStage,
        *,
        top: int,
        budget: Budget,
        progress: Callable[[str], None],
        retry_failed: bool = False,
    ) -> StageResult:
        assert (source, stage, top, retry_failed) == (7, BoardStage.RENDER, 3, True)
        progress("Rendering clip 4")
        started.set()
        assert finish.wait(timeout=5)
        return StageResult("Rendered and captioned 1 clip.")

    monkeypatch.setattr("montaj.dashboard_jobs.run_board_stage", fake_stage)
    assert jobs.running() is None
    jobs.submit_stage(
        StageSubmission(source=7, stage=BoardStage.RENDER, top=3, retry=True)
    )
    try:
        assert started.wait(timeout=5)
        with pytest.raises(ValueError, match="already running"):
            jobs.submit_stage(StageSubmission(source=7, stage=BoardStage.RENDER))
        with pytest.raises(ValueError, match="already running"):
            jobs.submit(Submission(resume=7))
        activity = jobs.snapshot()
        assert activity is not None and activity.stage == "Rendering clip 4"
        assert jobs.running() == (7, "render")
    finally:
        finish.set()
        threads[0].join(timeout=5)
    assert len(threads) == 1 and not threads[0].is_alive()
    activity = jobs.snapshot()
    assert activity is not None and not activity.running
    assert activity.summary == "Rendered and captioned 1 clip." and not activity.problem
    assert jobs.running() is None


@pytest.mark.parametrize("stage", sorted(PAID_STAGES))
def test_paid_stage_without_confirm_starts_nothing(
    db_path: Path, monkeypatch: pytest.MonkeyPatch, stage: BoardStage
) -> None:
    _add_source(db_path)
    jobs = JobQueue(Settings(data_dir=db_path.parent))
    threads = _capture_threads(monkeypatch)
    runner = Mock(return_value=StageResult("Done."))
    monkeypatch.setattr("montaj.dashboard_jobs.run_board_stage", runner)
    with pytest.raises(ValueError, match="Confirm the estimated cost"):
        jobs.submit_stage(StageSubmission(source=7, stage=stage))
    runner.assert_not_called()
    assert threads == [] and jobs.snapshot() is None
    jobs.submit_stage(StageSubmission(source=7, stage=stage, confirm=True))
    threads[0].join(timeout=5)
    runner.assert_called_once()


def test_free_stage_runs_without_confirm(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_source(db_path)
    jobs = JobQueue(Settings(data_dir=db_path.parent))
    threads = _capture_threads(monkeypatch)
    runner = Mock(return_value=StageResult("Rendered.", problem="Budget stop."))
    monkeypatch.setattr("montaj.dashboard_jobs.run_board_stage", runner)
    jobs.submit_stage(StageSubmission(source=7, stage=BoardStage.RENDER))
    threads[0].join(timeout=5)
    runner.assert_called_once()
    activity = jobs.snapshot()
    assert activity is not None and not activity.running
    assert (activity.summary, activity.problem) == ("Rendered.", "Budget stop.")


@pytest.mark.parametrize(
    ("allowed", "basis", "source"),
    [(False, "Creator approval", 7), (True, "  ", 7), (True, "Creator approval", 99)],
)
def test_stage_on_unpermitted_or_unknown_source_is_refused(
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    allowed: bool,
    basis: str,
    source: int,
) -> None:
    _add_source(db_path, allowed=allowed, basis=basis)
    jobs = JobQueue(Settings(data_dir=db_path.parent))
    threads = _capture_threads(monkeypatch)
    runner = Mock()
    monkeypatch.setattr("montaj.dashboard_jobs.run_board_stage", runner)
    with pytest.raises(ValueError, match="not found|permission"):
        jobs.submit_stage(StageSubmission(source=source, stage=BoardStage.RENDER))
    runner.assert_not_called()
    assert threads == [] and jobs.snapshot() is None


def test_locked_source_reports_holder_in_activity_problem(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_source(db_path)
    jobs = JobQueue(Settings(data_dir=db_path.parent))
    threads = _capture_threads(monkeypatch)
    monkeypatch.setattr(
        "montaj.dashboard_jobs.run_board_stage",
        Mock(side_effect=MediaError("Source 7 is locked, held by cut (pid 12).")),
    )
    jobs.submit_stage(StageSubmission(source=7, stage=BoardStage.RENDER))
    threads[0].join(timeout=5)
    activity = jobs.snapshot()
    assert activity is not None and not activity.running
    assert "held by cut" in activity.problem
    assert activity.summary == "Stopped during render."
    assert jobs.running() is None


def test_unexpected_stage_error_still_ends_the_job(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_source(db_path)
    jobs = JobQueue(Settings(data_dir=db_path.parent))
    threads = _capture_threads(monkeypatch)
    monkeypatch.setattr(
        "montaj.dashboard_jobs.run_board_stage",
        Mock(side_effect=RuntimeError("secret detail")),
    )
    jobs.submit_stage(StageSubmission(source=7, stage=BoardStage.RENDER))
    threads[0].join(timeout=5)
    activity = jobs.snapshot()
    assert activity is not None and not activity.running
    assert activity.summary == "Stopped during render."
    assert activity.problem and "secret detail" not in activity.problem


@pytest.mark.parametrize(
    ("stage", "until"),
    [
        (BoardStage.TRANSCRIBE, "transcribe"),
        (BoardStage.RANK, "rank"),
        (BoardStage.DRAFT, "draft"),
    ],
)
def test_estimate_prices_the_paid_stage(
    db_path: Path, monkeypatch: pytest.MonkeyPatch, stage: BoardStage, until: str
) -> None:
    jobs = JobQueue(Settings(data_dir=db_path.parent))
    seen: list[tuple[int, str, str, float]] = []

    def fake_estimate(
        settings: Settings, source: int, until: str, language: str, budget: Budget
    ) -> list[str]:
        seen.append((source, until, language, budget.per_run))
        return ["line"]

    monkeypatch.setattr("montaj.dashboard_jobs.estimate_run", fake_estimate)
    assert jobs.estimate(7, stage, 1.5) == ["line"]
    assert seen == [(7, until, "en", 1.5)]
    assert jobs.estimate(7, BoardStage.RENDER, None) == []
    assert len(seen) == 1


def test_running_reports_a_new_source_import_as_source_zero(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs = JobQueue(Settings(data_dir=db_path.parent))
    threads = _capture_threads(monkeypatch)
    importing, finish = Event(), Event()

    def fake_start(settings: Settings, location: str, **_: object) -> int:
        importing.set()
        assert finish.wait(timeout=5)
        return 7

    monkeypatch.setattr("montaj.dashboard_jobs.start_job", fake_start)
    monkeypatch.setattr(
        "montaj.dashboard_jobs.produce_batch",
        Mock(return_value=JobResult(7, 1, ready=[1])),
    )
    inbox = jobs.settings.data_dir / "inbox"
    inbox.mkdir()
    (inbox / "video.mp4").touch()
    jobs.submit(
        Submission(
            location="video.mp4",
            campaign=1,
            niche=Niche.HEALTH,
            basis="Creator approval",
            allowed=True,
        )
    )
    try:
        assert importing.wait(timeout=5)
        assert jobs.running() == (0, "batch")
    finally:
        finish.set()
        threads[0].join(timeout=5)
    assert jobs.running() is None


def _post_request(
    settings: Settings,
    jobs: JobQueue | None,
    fields: dict[str, object],
    *,
    origin: str = "http://127.0.0.1:8765",
    token: str | None = None,
    path: str = STAGE_PATH,
) -> Mock:
    handler = _handler(settings, jobs)
    request = object.__new__(handler)
    request.headers = Message()
    request.headers["Host"] = "127.0.0.1:8765"
    request.headers["Origin"] = origin
    request.headers["Content-Type"] = "application/x-www-form-urlencoded"
    valid = jobs.token if jobs is not None else ""
    supplied = token if token is not None else valid
    body = urlencode({"token": supplied, **fields}).encode()
    request.headers["Content-Length"] = str(len(body))
    request.path = path
    request.rfile = BytesIO(body)
    request.wfile = BytesIO()
    request.connection = Mock()
    request.send_error = Mock()
    request.send_response = Mock()
    request.send_header = Mock()
    request.end_headers = Mock()
    request.do_POST()
    return request  # type: ignore[return-value]


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
        budget=2.5,
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


def _home(
    db_path: Path,
    *,
    view: str = "",
    operations: bool = True,
    running: tuple[int, str] | None = None,
    run_spend: tuple[float, float] | None = None,
) -> str:
    _, _, body = dashboard_response(
        Settings(data_dir=db_path.parent),
        "/dashboard",
        view,
        operations=operations,
        running=running,
        token="board-token",
        run_spend=run_spend,
    )
    return body.decode()


def _rows(page: str) -> str:
    return page.split("<tbody>", 1)[1].split("</tbody>", 1)[0]


def _add_clip(db_path: Path, status: ClipStatus, source: int = 7) -> None:
    with connect(db_path) as db:
        insert(db, Clip(id=1, source_id=source, start_s=0, end_s=5, status=status))


def test_home_rows_show_stage_strip_and_next_action(db_path: Path) -> None:
    _add_source(db_path)
    page = _home(db_path)
    row = _rows(page)
    for label in ("Ingest", "Transcribe", "Candidates + rank", "Cut + captions"):
        assert label in row
    assert "Pending" in row and "Blocked" in row
    assert row.count("<button") == 1 and row.count("<form") == 1
    assert 'action="/dashboard/jobs"' in row and 'method="post"' in row
    assert 'name="token" value="board-token"' in row
    assert 'name="resume" value="7"' in row and ">Resume</button>" in row
    assert "<form" not in _rows(_home(db_path, operations=False))
    assert "<button" not in _rows(_home(db_path, operations=False))


@pytest.mark.parametrize(
    ("status", "label", "href"),
    [
        (ClipStatus.CHECKED, "Open review", 'href="/?source=7"'),
        (ClipStatus.APPROVED, "Export", 'href="/?source=7&amp;ready=1"'),
        (ClipStatus.NEEDS_FIX, "Fix flagged clips", 'href="/dashboard/sources/7"'),
    ],
)
def test_home_review_export_and_fix_actions_are_links(
    db_path: Path, status: ClipStatus, label: str, href: str
) -> None:
    _add_source(db_path)
    _add_clip(db_path, status)
    row = _rows(_home(db_path))
    assert f"{href}>{label}</a>" in row
    assert "<button" not in row and "<form" not in row


def test_home_top_bar_counts_attention_review_and_spend(db_path: Path) -> None:
    _add_source(db_path)
    _add_clip(db_path, ClipStatus.CHECKED)
    with connect(db_path) as db:
        insert(
            db,
            Source(
                id=8,
                path="two.mp4",
                title="Broken",
                niche=Niche.GAMES,
                source_allowed=True,
                speech_status="failed",
            ),
        )
        log_api_call(db, 7, "transcribe", Service.DEEPGRAM, Usage(seconds=12), 0.012345)
    idle = _home(db_path)
    assert "<dt>Needs attention</dt><dd>1</dd>" in idle
    assert "<dt>Clips awaiting review</dt><dd>1</dd>" in idle
    assert "<dt>Est. source API spend</dt><dd>$0.012345</dd>" in idle
    assert 'href="#new-job">New job</a>' in idle
    assert "Run spend" not in idle
    running = _home(db_path, running=(7, "render"), run_spend=(0.5, 5.0))
    assert "<dt>Run spend</dt><dd>$0.500000 of $5.00 run ceiling</dd>" in running
    assert "today" not in running.lower()


def test_unpermitted_source_row_has_no_run_buttons(db_path: Path) -> None:
    _add_source(db_path, allowed=False, basis="")
    page = _home(db_path)
    row = _rows(page)
    assert page.count("Record permission for this source first.") == 1
    assert "before processing" not in page
    assert "<form" not in row and "<button" not in row


@pytest.mark.parametrize(
    ("running", "message"),
    [
        ((2, "render"), "Another job is running (source 2)"),
        ((0, "batch"), "Another job is running"),
    ],
)
def test_run_buttons_are_disabled_while_a_job_runs(
    db_path: Path, running: tuple[int, str], message: str
) -> None:
    _add_source(db_path)
    with connect(db_path) as db:
        insert(
            db,
            Source(
                id=2, path="b.mp4", title="B", niche=Niche.GAMES, source_allowed=True
            ),
        )
    row = _rows(_home(db_path, running=running))
    assert row.count("<button") == row.count("<button disabled") == 2
    assert "<form" not in row
    assert message in row
    assert (running[0] == 0) == ("(source" not in row)


def test_home_escapes_titles_and_errors(db_path: Path) -> None:
    _add_source(db_path)
    with connect(db_path) as db:
        db.execute(
            "UPDATE sources SET title = ?, speech_status = 'failed', failure_note = ? WHERE id = 7",
            ("<script>alert(1)</script>", "<script>steal()</script>"),
        )
    page = _home(db_path)
    assert "<script" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "steal" not in page


def test_home_refreshes_only_while_running(db_path: Path) -> None:
    _add_source(db_path)
    assert 'http-equiv="refresh"' not in _home(db_path)
    page = _home(db_path, running=(7, "render"))
    assert '<meta http-equiv="refresh" content="3">' in page


def test_empty_board_points_to_new_job(db_path: Path) -> None:
    page = _home(db_path)
    assert "No sources yet" in page
    assert 'href="#new-job">New job</a>' in page
    assert "<table" not in page


def test_handle_preview_passes_live_job_state_to_the_board(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_source(db_path)
    settings = Settings(data_dir=db_path.parent)
    jobs = JobQueue(settings)
    threads = _capture_threads(monkeypatch)
    started, finish = Event(), Event()

    def blocked_stage(*_: object, **__: object) -> StageResult:
        started.set()
        assert finish.wait(timeout=5)
        return StageResult("Done.")

    monkeypatch.setattr("montaj.dashboard_jobs.run_board_stage", blocked_stage)
    idle = handle_preview(settings, "/dashboard?notice=Saved+it", jobs)[2].decode()
    assert f'name="token" value="{jobs.token}"' in idle and "Saved it" in idle
    assert 'http-equiv="refresh"' not in idle and jobs.run_spend() is None
    jobs.submit_stage(StageSubmission(source=7, stage=BoardStage.RENDER))
    try:
        assert started.wait(timeout=5)
        activity = jobs.snapshot()
        assert activity is not None
        with connect(db_path) as db:
            log_api_call(
                db,
                7,
                "draft",
                Service.DEEPGRAM,
                Usage(seconds=1),
                0.25,
                reservation=activity.run_id,
            )
        assert jobs.run_spend() == (0.25, settings.max_cost_per_run_usd)
        live = handle_preview(settings, "/dashboard", jobs)[2].decode()
        assert 'http-equiv="refresh"' in live and "<dt>Run spend</dt>" in live
        assert "Another job is running (source 7)" in live
    finally:
        finish.set()
        threads[0].join(timeout=5)
    read_only = handle_preview(settings, "/dashboard")[2].decode()
    assert "<form" not in read_only and 'http-equiv="refresh"' not in read_only


def _detail_page(
    db_path: Path,
    *,
    operations: bool = True,
    running: tuple[int, str] | None = None,
    live: LiveJob | None = None,
) -> str:
    status, _, body = dashboard_response(
        Settings(data_dir=db_path.parent),
        "/dashboard/sources/7",
        "",
        operations=operations,
        running=running,
        token="detail-token",
        live=live,
    )
    assert status == 200
    return body.decode()


def _imported(db_path: Path, *extra: str) -> None:
    folder = db_path.parent / "sources/7"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "meta.json").write_text(
        SourceMetadata(
            title="Interview", duration=72, video_hash="video", audio_hash="audio"
        ).model_dump_json(),
        encoding="utf-8",
    )
    for name in ("video.mp4", "audio.wav", *extra):
        (folder / name).write_bytes(b"fixture")


def _stage_lines(page: str) -> dict[str, str]:
    """The markup of each stage line, keyed by its label."""
    block = page.split('<ol class="steps stage-lines"', 1)[1].split("</ol>", 1)[0]
    lines = block.split("<li")[1:]
    return {
        line.split('class="cell-name">', 1)[1].split("<", 1)[0]: line for line in lines
    }


def test_detail_lists_every_stage_with_status_and_action(db_path: Path) -> None:
    _add_source(db_path)
    _imported(db_path)
    lines = _stage_lines(_detail_page(db_path))
    assert list(lines) == [
        "Ingest",
        "Transcribe",
        "Candidates + rank",
        "Cut + captions",
        "Draft + check",
        "Review",
        "Export",
    ]
    assert "Done" in lines["Ingest"] and "<a" not in lines["Ingest"]
    assert (
        "Pending" in lines["Transcribe"] and "Run (confirm cost)" in lines["Transcribe"]
    )
    for label in ("Candidates + rank", "Cut + captions", "Draft + check"):
        assert "Blocked" in lines[label] and "Transcribe first" in lines[label]
        assert "<a" not in lines[label] and "<button" not in lines[label]
    for label in ("Review", "Export"):
        assert "<a" not in lines[label] and "<button" not in lines[label]
    with connect(db_path) as db:
        db.execute("UPDATE sources SET speech_status = 'failed' WHERE id = 7")
    failed = _stage_lines(_detail_page(db_path))["Transcribe"]
    assert "Failed" in failed and "Montaj recorded a source failure" in failed
    assert "Retry (confirm cost)" in failed and "retry=yes" in failed
    assert "Run (confirm cost)" not in failed


def test_paid_stage_links_to_confirm_and_free_stage_posts_directly(
    db_path: Path,
) -> None:
    _add_source(db_path)
    _imported(db_path)
    paid = _stage_lines(_detail_page(db_path))["Transcribe"]
    assert 'href="/dashboard/sources/7/confirm?stage=transcribe"' in paid
    assert "<form" not in paid and "<button" not in paid
    _imported(db_path, "transcript.json", "candidates.json", "rank.json")
    free = _stage_lines(_detail_page(db_path))["Cut + captions"]
    assert 'action="/dashboard/stage" method="post"' in free
    assert 'name="token" value="detail-token"' in free
    assert 'name="source" value="7"' in free and 'name="stage" value="render"' in free
    assert "retry" not in free and "confirm" not in free
    assert free.count("<button") == 1 and "/confirm" not in free


def test_detail_read_only_shows_status_but_no_controls(db_path: Path) -> None:
    _add_source(db_path)
    _imported(db_path)
    page = _detail_page(db_path, operations=False)
    assert "Pending" in _stage_lines(page)["Transcribe"]
    assert "<form" not in page and "<button" not in page and "/confirm" not in page


def test_detail_buttons_are_disabled_while_a_job_runs(db_path: Path) -> None:
    _add_source(db_path)
    _imported(db_path, "transcript.json", "candidates.json", "rank.json")
    page = _detail_page(db_path, running=(2, "render"))
    free = _stage_lines(page)["Cut + captions"]
    assert "<button disabled" in free and "<form" not in free
    assert "Another job is running (source 2)" in free
    assert 'aria-describedby="why-render"' in free and 'id="why-render"' in free
    assert "<form" not in page and "/confirm" not in page
    (db_path.parent / "sources/7/transcript.json").unlink()
    paid = _stage_lines(_detail_page(db_path, running=(0, "batch")))["Transcribe"]
    assert "<button disabled" in paid and "/confirm" not in paid
    assert "Another job is running" in paid and "(source" not in paid


def test_detail_links_to_clip_preview(db_path: Path) -> None:
    _add_source(db_path)
    _imported(db_path)
    assert "Preview clips" not in _detail_page(db_path)
    _add_clip(db_path, ClipStatus.CHECKED)
    page = _detail_page(db_path)
    assert 'href="/?source=7">Preview clips' in page
    lines = _stage_lines(page)
    assert 'href="/?source=7"' in lines["Review"] and "Open review" in lines["Review"]
    assert "Open review" not in lines["Export"]


def test_detail_header_shows_campaign_and_source_spend(db_path: Path) -> None:
    with connect(db_path) as db:
        db.execute(
            "INSERT INTO campaigns (id, name, platform, rules_json) VALUES (3, ?, 'tiktok', '{}')",
            ("<b>Spring</b>",),
        )
        insert(
            db,
            Source(
                id=7,
                path="input.mp4",
                title="Interview",
                niche=Niche.HEALTH,
                campaign_id=3,
                source_allowed=True,
                allowed_basis="Creator approval",
            ),
        )
        log_api_call(db, 7, "transcribe", Service.DEEPGRAM, Usage(seconds=1), 0.5)
    page = _detail_page(db_path)
    assert "&lt;b&gt;Spring&lt;/b&gt;" in page and "<b>Spring" not in page
    assert "$0.500000 of $2.00 source ceiling" in page


def test_detail_shows_live_job_panel(db_path: Path) -> None:
    _add_source(db_path)
    running = LiveJob(7, "Transcribing", True, 12.4, 0.25, "", "")
    page = _detail_page(db_path, running=(7, "transcribe"), live=running)
    assert "Transcribing" in page and "Elapsed 12 s" in page and "$0.250000" in page
    assert '<meta http-equiv="refresh" content="3">' in page
    done = LiveJob(
        7,
        "Starting",
        False,
        3.0,
        0.1,
        "Stopped during transcribe.",
        "<i>Another process holds the lock (pid 5)</i>",
    )
    finished = _detail_page(db_path, live=done)
    assert "Stopped during transcribe." in finished
    assert "&lt;i&gt;Another process holds the lock (pid 5)&lt;/i&gt;" in finished
    assert "<i>" not in finished and 'http-equiv="refresh"' not in finished
    other = LiveJob(8, "x", True, 1, 0, "", "")
    assert "Live job" not in _detail_page(db_path, live=other)
    importing = LiveJob(None, "x", True, 1, 0, "", "")
    assert "Live job" not in _detail_page(db_path, live=importing)
    assert "Live job" not in _detail_page(db_path)


def test_job_queue_reports_its_live_job_with_run_spend(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_source(db_path)
    settings = Settings(data_dir=db_path.parent)
    jobs = JobQueue(settings)
    assert jobs.live_job() is None
    threads = _capture_threads(monkeypatch)
    started, finish = Event(), Event()

    def blocked_stage(*_: object, **__: object) -> StageResult:
        started.set()
        assert finish.wait(timeout=5)
        return StageResult("Cut 2 clips.", "One clip skipped.")

    monkeypatch.setattr("montaj.dashboard_jobs.run_board_stage", blocked_stage)
    jobs.submit_stage(StageSubmission(source=7, stage=BoardStage.RENDER))
    try:
        assert started.wait(timeout=5)
        activity = jobs.snapshot()
        assert activity is not None
        with connect(db_path) as db:
            log_api_call(
                db,
                7,
                "draft",
                Service.DEEPGRAM,
                Usage(seconds=1),
                0.25,
                reservation=activity.run_id,
            )
        live = jobs.live_job()
        assert live is not None and live.running and live.source_id == 7
        assert live.spend == 0.25 and live.elapsed_s >= 0
        detail = handle_preview(settings, "/dashboard/sources/7", jobs)[2].decode()
        assert "Elapsed" in detail and "$0.250000" in detail
    finally:
        finish.set()
        threads[0].join(timeout=5)
    done = jobs.live_job()
    assert done is not None and not done.running
    assert (done.summary, done.problem) == ("Cut 2 clips.", "One clip skipped.")


def _confirm(
    settings: Settings, jobs: JobQueue | None, source: int, query: str
) -> tuple[int, bytes]:
    status, _, body = handle_preview(
        settings, f"/dashboard/sources/{source}/confirm?{query}", jobs
    )
    return status, body


def test_confirm_page_shows_estimate_lines_and_confirm_form(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_source(db_path)
    settings = Settings(data_dir=db_path.parent)
    jobs = JobQueue(settings)
    seen: list[tuple[int, str, float]] = []

    def fake_estimate(
        settings: Settings, source: int, until: str, language: str, budget: Budget
    ) -> list[str]:
        seen.append((source, until, budget.per_run))
        return ["Transcription: $0.10", "Total <b>$0.10</b>"]

    monkeypatch.setattr("montaj.dashboard_jobs.estimate_run", fake_estimate)
    status, body = _confirm(settings, jobs, 7, "stage=rank")
    page = body.decode()
    assert status == 200 and seen == [(7, "rank", settings.max_cost_per_run_usd)]
    assert "Transcription: $0.10" in page and "Total &lt;b&gt;$0.10&lt;/b&gt;" in page
    assert "<b>" not in page
    assert 'action="/dashboard/stage" method="post"' in page
    for field in (
        f'name="token" value="{jobs.token}"',
        'name="source" value="7"',
        'name="stage" value="rank"',
        'name="confirm" value="yes"',
    ):
        assert field in page
    assert 'name="retry"' not in page and page.count("<button") == 1
    assert 'href="/dashboard/sources/7">Cancel' in page
    retry = _confirm(settings, jobs, 7, "stage=rank&retry=yes")[1].decode()
    assert 'name="retry" value="yes"' in retry


@pytest.mark.parametrize(
    "error",
    [
        ValueError("raw /secret/path detail"),
        MediaError("raw ffmpeg detail"),
        OSError("raw os detail"),
    ],
)
def test_confirm_page_hides_a_failed_estimate(
    db_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    _add_source(db_path)
    settings = Settings(data_dir=db_path.parent)

    def failing(*_: object, **__: object) -> list[str]:
        raise error

    monkeypatch.setattr("montaj.dashboard_jobs.estimate_run", failing)
    status, body = _confirm(settings, JobQueue(settings), 7, "stage=transcribe")
    page = body.decode()
    assert status == 200
    assert "Estimate unavailable. Check the source before running this stage." in page
    assert "raw" not in page and "<form" not in page and "<button" not in page
    assert 'href="/dashboard/sources/7"' in page


@pytest.mark.parametrize(
    ("source", "query"),
    [(7, "stage=bogus"), (7, ""), (7, "stage=render"), (99, "stage=rank")],
)
def test_confirm_unknown_stage_is_404(
    db_path: Path, monkeypatch: pytest.MonkeyPatch, source: int, query: str
) -> None:
    _add_source(db_path)
    settings = Settings(data_dir=db_path.parent)
    monkeypatch.setattr(
        "montaj.dashboard_jobs.estimate_run", lambda *a, **k: ["never shown"]
    )
    status, body = _confirm(settings, JobQueue(settings), source, query)
    assert status == 404 and b"never shown" not in body
    assert b"not found" in body.lower()


def test_confirm_page_is_not_served_read_only(db_path: Path) -> None:
    _add_source(db_path)
    status, _ = _confirm(Settings(data_dir=db_path.parent), None, 7, "stage=rank")
    assert status == 404


def test_detail_for_an_unpermitted_source_has_no_actions_and_says_why(
    db_path: Path,
) -> None:
    _add_source(db_path, allowed=False, basis="")
    _add_clip(db_path, ClipStatus.CHECKED)
    _imported(db_path)
    page = _detail_page(db_path)
    for absent in ("<form", "<button", "/confirm", "Open review", "Preview clips"):
        assert absent not in page
    lines = _stage_lines(page)
    assert len(lines) == 7
    assert all("Blocked" in line for line in lines.values())
    # Each stage line gives the reason, so it appears once per line.
    assert page.count("Record permission for this source first.") == len(lines)
    assert "Record permission for this source before processing it." in page


def test_detail_for_a_source_without_a_basis_still_lists_run_but_the_server_refuses(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Known UX gap: the board only checks source_allowed, so a blank basis still
    # shows Run. submit_stage and the confirm page both refuse such a source.
    _add_source(db_path, allowed=True, basis="  ")
    _imported(db_path)
    page = _detail_page(db_path)
    assert "Run (confirm cost)" in _stage_lines(page)["Transcribe"]
    settings = Settings(data_dir=db_path.parent)
    jobs = JobQueue(settings)
    monkeypatch.setattr(
        "montaj.dashboard_jobs.estimate_run", lambda *a, **k: ["never shown"]
    )
    status, body = _confirm(settings, jobs, 7, "stage=transcribe")
    assert status == 200 and b"never shown" not in body and b"<form" not in body
    assert b"Estimate unavailable" in body
    with pytest.raises(ValueError, match="recorded permission"):
        jobs.submit_stage(StageSubmission(source=7, stage=BoardStage.RENDER))


def _add_campaign(db_path: Path, name: str = "Spring", campaign_id: int = 3) -> None:
    with connect(db_path) as db:
        db.execute(
            "INSERT INTO campaigns (id, name, platform, rules_json) VALUES (?, ?, 'tiktok', '{}')",
            (campaign_id, name),
        )


def _board(db_path: Path, jobs: JobQueue | None, target: str = "/dashboard") -> str:
    settings = Settings(data_dir=db_path.parent)
    return handle_preview(settings, target, jobs)[2].decode()


def test_board_has_new_job_form_with_permission_fields(db_path: Path) -> None:
    _add_campaign(db_path)
    jobs = JobQueue(Settings(data_dir=db_path.parent))
    page = _board(db_path, jobs)
    assert '<details id="new-job" class="panel job-panel">' in page
    assert 'href="#new-job">New job</a>' in page
    form = page.split('<form class="job-form" action="/dashboard/jobs"', 1)[1]
    form = form.split("</form>", 1)[0]
    assert 'method="post"' in form and f'name="token" value="{jobs.token}"' in form
    assert 'name="location" maxlength="2048" required' in form
    assert '<select name="campaign" required><option value="3">Spring</option>' in form
    assert '<select name="niche" required>' in form
    assert f'<option value="{Niche.HEALTH.value}">' in form
    assert 'name="basis" maxlength="200" required' in form
    assert 'type="checkbox" name="allowed" value="true" required' in form
    assert "I have permission to use this video" in form
    assert 'name="top" type="number" min="1" max="' in form and 'value="10"' in form
    assert 'name="budget" type="number" min="0" step="0.01"' in form
    assert "Resume a source" not in page


def test_new_job_form_escapes_campaign_names(db_path: Path) -> None:
    _add_campaign(db_path, "<img src=x onerror=alert(1)>")
    page = _board(db_path, JobQueue(Settings(data_dir=db_path.parent)))
    assert "&lt;img src=x onerror=alert(1)&gt;" in page and "<img" not in page


def test_new_job_panel_without_campaigns_explains_how_to_add_one(
    db_path: Path,
) -> None:
    page = _board(db_path, JobQueue(Settings(data_dir=db_path.parent)))
    assert "montaj campaign add" in page and 'name="location"' not in page


def test_new_job_form_is_absent_on_a_read_only_server(db_path: Path) -> None:
    _add_campaign(db_path)
    page = _board(db_path, None)
    assert "new-job" not in page and "<form" not in page and "Start job" not in page


def test_new_job_form_is_replaced_while_a_job_runs(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_campaign(db_path)
    _add_source(db_path)
    jobs = JobQueue(Settings(data_dir=db_path.parent))
    threads = _capture_threads(monkeypatch)
    started, finish = Event(), Event()

    def blocked_stage(*_: object, **__: object) -> StageResult:
        started.set()
        assert finish.wait(timeout=5)
        return StageResult("Done.")

    monkeypatch.setattr("montaj.dashboard_jobs.run_board_stage", blocked_stage)
    jobs.submit_stage(StageSubmission(source=7, stage=BoardStage.RENDER))
    try:
        assert started.wait(timeout=5)
        page = _board(db_path, jobs)
    finally:
        finish.set()
        threads[0].join(timeout=5)
    assert "The worker is preparing a batch." in page
    assert 'name="location"' not in page and "Start job" not in page
    assert "Running:" in page and "Live job" in page


def test_board_shows_the_latest_finished_job_with_a_review_link(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_source(db_path)
    jobs = JobQueue(Settings(data_dir=db_path.parent))
    threads = _capture_threads(monkeypatch)
    monkeypatch.setattr(
        "montaj.dashboard_jobs.run_board_stage", lambda *a, **k: StageResult("<Done>")
    )
    jobs.submit_stage(StageSubmission(source=7, stage=BoardStage.RENDER))
    threads[0].join(timeout=5)
    page = _board(db_path, jobs)
    assert "Latest job" in page and "&lt;Done&gt;" in page and "<Done>" not in page
    assert 'href="/?source=7&amp;ready=1">Review clips</a>' in page
    assert "Latest job" not in _board(db_path, None)


def test_jobs_path_serves_the_board(db_path: Path) -> None:
    _add_campaign(db_path)
    _add_source(db_path)
    settings = Settings(data_dir=db_path.parent)
    jobs = JobQueue(settings)
    board = handle_preview(settings, "/dashboard?notice=Hello", jobs)
    served = handle_preview(settings, f"{JOBS_PATH}?notice=Hello", jobs)
    assert served[0] == 200 and served[1].startswith("text/html")
    assert served == board
    page = served[2].decode()
    assert "Production board" in page and "Hello" in page
    assert "Production jobs" not in page


def test_new_job_error_rerenders_board_with_notice(db_path: Path) -> None:
    _add_campaign(db_path)
    settings = Settings(data_dir=db_path.parent)
    jobs = JobQueue(settings)
    fields = {
        "location": "https://example.com/video",
        "campaign": 3,
        "niche": "health",
        "basis": "Creator approval",
        "allowed": "true",
    }
    request = _post_request(settings, jobs, fields, path=JOBS_PATH)
    request.send_response.assert_called_once_with(400)
    page = request.wfile.getvalue().decode()
    message = "Enter an individual YouTube video URL or use the inbox."
    assert f'<p class="notice" role="status">{message}</p>' in page
    assert '<details id="new-job" class="panel job-panel" open>' in page
    assert "Production board" in page and "Traceback" not in page
    assert jobs.snapshot() is None


def test_new_job_notice_is_escaped(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(data_dir=db_path.parent)
    jobs = JobQueue(settings)
    refusal = ValueError("<script>alert(1)</script>")
    monkeypatch.setattr(jobs, "submit", Mock(side_effect=refusal))
    request = _post_request(settings, jobs, {"resume": 1}, path=JOBS_PATH)
    page = request.wfile.getvalue().decode()
    assert "&lt;script&gt;" in page and "<script>" not in page


def test_valid_job_post_redirects_to_the_board(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(data_dir=db_path.parent)
    jobs = JobQueue(settings)
    monkeypatch.setattr(jobs, "submit", Mock())
    request = _post_request(settings, jobs, {"resume": 1}, path=JOBS_PATH)
    request.send_response.assert_called_once_with(303)
    request.send_header.assert_any_call("Location", "/dashboard")


def test_nav_no_longer_links_production_jobs(db_path: Path) -> None:
    _add_campaign(db_path)
    _add_source(db_path)
    settings = Settings(data_dir=db_path.parent)
    jobs = JobQueue(settings)
    for target in ("/dashboard", "/dashboard/sources/7", "/dashboard?view=attention"):
        page = handle_preview(settings, target, jobs)[2].decode()
        assert "Production jobs" not in page and 'href="/dashboard/jobs"' not in page
