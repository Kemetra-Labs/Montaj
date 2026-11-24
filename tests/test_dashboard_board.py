import shutil
from pathlib import Path
from threading import Event
from unittest.mock import Mock

import pytest

from montaj import dashboard as dashboard_module
from montaj.config import Settings
from montaj.costs import Service, Usage, log_api_call
from montaj.dashboard_jobs import (
    JOBS_PATH,
    JobQueue,
    StageSubmission,
)
from montaj.db import connect, insert
from montaj.models import ClipStatus, Niche, Source
from montaj.preview import handle_preview
from montaj.stage_jobs import BoardStage, StageResult
from tests.dashboard_helpers import (
    _add_clip,
    _add_source,
    _capture_threads,
    _home,
    _imported,
    _post_request,
    _rows,
)


def test_home_rows_show_stage_strip_and_next_action(db_path: Path) -> None:
    _add_source(db_path)
    _imported(db_path)
    page = _home(db_path)
    row = _rows(page)
    for label in ("Ingest", "Transcribe", "Candidates + rank", "Cut + captions"):
        assert label in row
    assert "Pending" in row and "Blocked" in row
    # Resume spends API money, so it opens the cost confirmation first.
    assert "<button" not in row and "<form" not in row
    assert 'href="/dashboard/sources/7/confirm?stage=batch">Resume</a>' in row
    read_only = _rows(_home(db_path, operations=False))
    assert "<form" not in read_only and "<button" not in read_only
    assert "/confirm" not in read_only


@pytest.mark.parametrize(
    ("status", "label", "href"),
    [
        (ClipStatus.CHECKED, "Open review", 'href="/?source=7"'),
        (ClipStatus.APPROVED, "Export", 'href="/?source=7&amp;ready=1"'),
        (ClipStatus.NEEDS_FIX, "Review flagged clips", 'href="/?source=7"'),
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
    assert '>Needs attention</a></dt><dd class="figure">1</dd>' in idle
    assert '>Clips awaiting review</a></dt><dd class="figure">1</dd>' in idle
    assert '<dt>Est. API spend</dt><dd class="figure">$0.01</dd>' in idle
    assert "Run spend" not in idle
    running = _home(db_path, running=(7, "render"), run_spend=(0.5, 5.0))
    assert (
        '<dt>Run spend</dt><dd class="figure">$0.50</dd>'
        '<dd class="note">of $5.00 run ceiling</dd>'
    ) in running
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
    _imported(db_path)
    shutil.copytree(db_path.parent / "sources/7", db_path.parent / "sources/2")
    with connect(db_path) as db:
        insert(
            db,
            Source(
                id=2,
                path="b.mp4",
                title="B",
                niche=Niche.GAMES,
                source_allowed=True,
                allowed_basis="Creator approval",
            ),
        )
    row = _rows(_home(db_path, running=running))
    assert row.count("<button") == row.count("<button disabled") == 2
    assert "<form" not in row
    assert message in row
    assert (running[0] == 0) == ("(source" not in row)
    # The source whose job runs says so instead of naming itself as another job.
    assert (running[0] == 2) == ("Running</span>" in row)
    assert "(source 7)" not in row


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
    assert 'id="new-job"' in page
    assert "<table" not in page


def test_handle_preview_passes_live_job_state_to_the_board(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_source(db_path)
    _imported(db_path)
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
    # A notice in the address is never shown: only a refused POST passes one.
    assert "confirm?stage=batch" in idle and "Saved it" not in idle
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
        assert "Running</span>" in _rows(live)
        assert "Another job is running" not in _rows(live)
    finally:
        finish.set()
        threads[0].join(timeout=5)
    read_only = handle_preview(settings, "/dashboard")[2].decode()
    assert "<form" not in read_only and 'http-equiv="refresh"' not in read_only


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
    assert 'id="new-job"' in page
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
    assert 'name="budget" type="number" min="0" max="0.50" step="0.01"' in form
    assert (
        '<input type="checkbox" name="confirm" value="yes" required> I understand '
        "this run spends API credits, up to the run ceiling of $0.50 and the "
        "source ceiling of $2.00"
    ) in form
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
    assert "Pipeline Control" in page and "Hello" not in page
    detail = handle_preview(settings, "/dashboard/sources/7?notice=Hello", jobs)
    assert b"Hello" not in detail[2]
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
        "confirm": "yes",
    }
    request = _post_request(settings, jobs, fields, path=JOBS_PATH)
    request.send_response.assert_called_once_with(400)
    page = request.wfile.getvalue().decode()
    message = "Enter an individual YouTube video URL or use the inbox."
    assert f'<p class="notice" role="status">{message}</p>' in page
    assert '<details id="new-job" class="panel job-panel" open>' in page
    assert "Pipeline Control" in page and "Traceback" not in page
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


def test_home_row_without_a_basis_has_no_actions_and_says_why(db_path: Path) -> None:
    _add_source(db_path, allowed=True, basis=" ")
    _imported(db_path)
    page = _home(db_path)
    assert page.count("Record a permission basis for this source first.") == 1
    assert "<form" not in _rows(page) and "<button" not in _rows(page)


def test_restarted_dashboard_shows_checkpoints_and_resume(db_path: Path) -> None:
    _add_source(db_path)
    _imported(db_path, "transcript.json", "candidates.json", "rank.json")
    settings = Settings(data_dir=db_path.parent)
    restarted = JobQueue(settings)
    assert restarted.running() is None and restarted.live_job() is None
    page = handle_preview(settings, "/dashboard", restarted)[2].decode()
    row = _rows(page)
    for label in ("Transcribe", "Candidates + rank"):
        assert f'<li class="cell done"><span class="cell-name">{label}</span>' in row
    assert 'href="/dashboard/sources/7/confirm?stage=batch">Resume</a>' in row
    assert "<button disabled" not in row and "Live job" not in page
    detail = handle_preview(settings, "/dashboard/sources/7", restarted)[2].decode()
    assert detail.count(">Saved<") == 3


def test_finished_job_with_a_problem_has_no_review_link(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_source(db_path)
    jobs = JobQueue(Settings(data_dir=db_path.parent))
    threads = _capture_threads(monkeypatch)
    monkeypatch.setattr(
        "montaj.dashboard_jobs.run_board_stage",
        lambda *a, **k: StageResult("Rendered 1 clip.", problem="One clip skipped."),
    )
    jobs.submit_stage(StageSubmission(source=7, stage=BoardStage.RENDER))
    threads[0].join(timeout=5)
    for target in ("/dashboard", "/dashboard/sources/7"):
        page = _board(db_path, jobs, target)
        assert "One clip skipped." in page and ">Review clips</a>" not in page


def test_campaigns_are_not_queried_while_a_job_runs(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_campaign(db_path)
    queried: list[bool] = []
    original = dashboard_module._campaign_choices

    def spy(settings: Settings) -> tuple[tuple[int, str], ...]:
        queried.append(True)
        return original(settings)

    monkeypatch.setattr(dashboard_module, "_campaign_choices", spy)
    _home(db_path, running=(7, "render"))
    assert queried == []
    _home(db_path)
    assert queried == [True]


def test_jobs_path_on_a_read_only_server_serves_the_board_without_forms(
    db_path: Path,
) -> None:
    _add_campaign(db_path)
    status, content_type, body = handle_preview(
        Settings(data_dir=db_path.parent), JOBS_PATH, None
    )
    assert status == 200 and content_type.startswith("text/html")
    assert b"Pipeline Control" in body and b"<form" not in body


def test_new_job_without_confirm_is_refused_and_starts_nothing(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_campaign(db_path)
    settings = Settings(data_dir=db_path.parent)
    jobs = JobQueue(settings)
    threads = _capture_threads(monkeypatch)
    (settings.data_dir / "inbox").mkdir()
    (settings.data_dir / "inbox/video.mp4").touch()
    fields = {
        "location": "video.mp4",
        "campaign": 3,
        "niche": "health",
        "basis": "Creator approval",
        "allowed": "true",
    }
    request = _post_request(settings, jobs, fields, path=JOBS_PATH)
    request.send_response.assert_called_once_with(400)
    page = request.wfile.getvalue().decode()
    assert "Confirm the estimated cost first." in page
    assert threads == [] and jobs.snapshot() is None


def test_job_post_passes_the_confirmation_through(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(data_dir=db_path.parent)
    jobs = JobQueue(settings)
    submit = Mock()
    monkeypatch.setattr(jobs, "submit", submit)
    _post_request(
        settings, jobs, {"resume": 7, "top": 10, "confirm": "yes"}, path=JOBS_PATH
    )
    submission = submit.call_args.args[0]
    assert (submission.resume, submission.top, submission.confirm) == (7, 10, True)
    _post_request(settings, jobs, {"resume": 7, "confirm": "no"}, path=JOBS_PATH)
    assert submit.call_args.args[0].confirm is False


def test_home_figures_come_from_one_definition(db_path: Path) -> None:
    _add_source(db_path)
    _add_clip(db_path, ClipStatus.CHECKED)
    with connect(db_path) as db:
        insert(
            db,
            Source(
                id=8,
                path="other.mp4",
                title="Other",
                niche=Niche.HEALTH,
                source_allowed=False,
                allowed_basis=None,
            ),
        )
    _add_clip(db_path, ClipStatus.CHECKED, source=8, clip_id=2)
    page = _home(db_path)
    # The unpermitted source needs attention and does not count as awaiting review.
    assert '>Needs attention</a></dt><dd class="figure">1</dd>' in page
    assert '>Clips awaiting review</a></dt><dd class="figure">1</dd>' in page
    assert "Needs attention</span><strong>1</strong>" in page
    assert "Clips awaiting review</span><strong>1</strong>" in page
