from collections.abc import Callable
from pathlib import Path
from threading import Event, Thread
from unittest.mock import Mock

import pytest

from montaj.budget import Budget
from montaj.config import Settings
from montaj.costs import Service, Usage, log_api_call
from montaj.dashboard_jobs import (
    JobQueue,
    StageSubmission,
    Submission,
)
from montaj.db import connect
from montaj.job import JobResult
from montaj.media import MediaError
from montaj.models import Niche
from montaj.preview import handle_preview
from montaj.stage_jobs import PAID_STAGES, BoardStage, StageResult
from tests.dashboard_helpers import (
    _add_source,
    _capture_threads,
)


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
