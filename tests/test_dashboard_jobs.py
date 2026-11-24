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
from montaj.ingest import SourceError
from montaj.job import JobResult
from montaj.media import MediaError
from montaj.models import Niche, Source
from montaj.preview import handle_preview
from montaj.stage_jobs import PAID_STAGES, BoardStage, StageResult
from montaj.transcribe import SpeechError
from tests.dashboard_helpers import (
    _add_source,
    _capture_threads,
)


def test_dashboard_job_progress_is_per_server_and_resume_uses_source_id(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_source(db_path)
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
    jobs.submit(Submission(resume=7, top=2, confirm=True))
    try:
        assert started.wait(timeout=5)
        with pytest.raises(ValueError, match="already running"):
            jobs.submit(Submission(resume=7, top=2, confirm=True))
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
    restarted.submit(Submission(resume=7, top=2, confirm=True))
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
            jobs.submit(Submission(resume=7, confirm=True))
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
        return list(_ESTIMATE_LINES)

    monkeypatch.setattr("montaj.dashboard_jobs.estimate_run", fake_estimate)
    assert jobs.estimate(7, stage, 1.5) == _STAGE_LINES[stage]
    assert seen == [(7, until, "en", 1.5)]
    assert jobs.estimate(7, BoardStage.RENDER, None) == []
    assert len(seen) == 1


_ESTIMATE_LINES = [
    "Source 7 prior estimated API spend: $0.000000",
    "Run ceiling: $1.500000 | Source ceiling: $1.500000",
    "transcribe: $0.120000 for one full audio pass (diarization off)",
    "Full transcription pass fits the current budget.",
    "candidates: remaining cost depends on inputs and cache reuse",
    "rank: remaining cost depends on inputs and cache reuse",
    "draft: remaining cost depends on inputs and cache reuse",
]
_CAPS = "spend stops at the run ceiling $1.50 and the source ceiling $1.50."
_STAGE_LINES = {
    BoardStage.TRANSCRIBE: _ESTIMATE_LINES[:4],
    BoardStage.RANK: [
        *_ESTIMATE_LINES[:2],
        *_ESTIMATE_LINES[4:6],
        f"No offline estimate for candidates and rank; {_CAPS}",
    ],
    BoardStage.DRAFT: [
        *_ESTIMATE_LINES[:2],
        _ESTIMATE_LINES[6],
        f"No offline estimate for draft; {_CAPS}",
    ],
}


def test_batch_estimate_covers_every_paid_stage_and_states_the_caps(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(data_dir=db_path.parent)
    jobs = JobQueue(settings)
    seen: list[tuple[str, float]] = []

    def fake_estimate(
        settings: Settings, source: int, until: str, language: str, budget: Budget
    ) -> list[str]:
        seen.append((until, budget.per_run))
        return list(_ESTIMATE_LINES)

    monkeypatch.setattr("montaj.dashboard_jobs.estimate_run", fake_estimate)
    lines = jobs.estimate_batch(7)
    assert seen == [("draft", settings.max_cost_per_run_usd)]
    assert lines[:-1] == _ESTIMATE_LINES
    run, source = settings.max_cost_per_run_usd, settings.max_cost_per_source_usd
    assert lines[-1] == (
        "No offline estimate for candidates, rank and draft; spend stops at the "
        f"run ceiling ${run:.2f} and the source ceiling ${source:.2f}."
    )


@pytest.mark.parametrize("resume", [7, None])
def test_submit_without_confirm_starts_nothing(
    db_path: Path, monkeypatch: pytest.MonkeyPatch, resume: int | None
) -> None:
    _add_source(db_path)
    jobs = JobQueue(Settings(data_dir=db_path.parent))
    threads = _capture_threads(monkeypatch)
    batch, start = Mock(), Mock()
    monkeypatch.setattr("montaj.dashboard_jobs.produce_batch", batch)
    monkeypatch.setattr("montaj.dashboard_jobs.start_job", start)
    submission = Submission(
        resume=resume,
        location="https://youtu.be/video_id001",
        campaign=1,
        niche=Niche.HEALTH,
        basis="Creator approval",
        allowed=True,
    )
    with pytest.raises(ValueError, match="Confirm the estimated cost first."):
        jobs.submit(submission)
    batch.assert_not_called()
    start.assert_not_called()
    assert threads == [] and jobs.snapshot() is None


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
            confirm=True,
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
        assert "Elapsed" in detail and "$0.25" in detail
        assert "$0.250000" not in detail
    finally:
        finish.set()
        threads[0].join(timeout=5)
    done = jobs.live_job()
    assert done is not None and not done.running
    assert (done.summary, done.problem) == ("Cut 2 clips.", "One clip skipped.")


@pytest.mark.parametrize(
    ("allowed", "basis", "source"),
    [(False, "Creator approval", 7), (True, "  ", 7), (True, "Creator approval", 99)],
)
def test_resume_of_an_unpermitted_or_unknown_source_is_refused_up_front(
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    allowed: bool,
    basis: str,
    source: int,
) -> None:
    _add_source(db_path, allowed=allowed, basis=basis)
    jobs = JobQueue(Settings(data_dir=db_path.parent))
    threads = _capture_threads(monkeypatch)
    batch = Mock()
    monkeypatch.setattr("montaj.dashboard_jobs.produce_batch", batch)
    with pytest.raises(ValueError, match="not found|recorded permission"):
        jobs.submit(Submission(resume=source, confirm=True))
    batch.assert_not_called()
    assert threads == [] and jobs.snapshot() is None


class _HeldThread:
    """A thread stand-in that never starts, so a test can run its target."""

    def __init__(
        self, *, target: Callable[..., None], args: tuple[object, ...], daemon: bool
    ) -> None:
        self.target, self.args = target, args

    def start(self) -> None:
        return None


def _held_threads(monkeypatch: pytest.MonkeyPatch) -> list[_HeldThread]:
    held: list[_HeldThread] = []

    def make(**kwargs: object) -> _HeldThread:
        thread = _HeldThread(**kwargs)  # type: ignore[arg-type]
        held.append(thread)
        return thread

    monkeypatch.setattr("montaj.dashboard_jobs.Thread", make)
    return held


def _submit_both(jobs: JobQueue, kind: str) -> None:
    if kind == "batch":
        jobs.submit(Submission(resume=7, confirm=True))
    else:
        jobs.submit_stage(StageSubmission(source=7, stage=BoardStage.RENDER))


def _invalid_record() -> None:
    Source.model_validate({"id": "not a number"})


def _raiser(error: BaseException | Callable[[], None]) -> Callable[..., object]:
    def fail(*_: object, **__: object) -> object:
        if callable(error) and not isinstance(error, BaseException):
            error()
        raise error  # type: ignore[misc]

    return fail


_RUNNERS = {
    "batch": "montaj.dashboard_jobs.produce_batch",
    "stage": "montaj.dashboard_jobs.run_board_stage",
}


@pytest.mark.parametrize("kind", ["batch", "stage"])
@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("secret detail"),
        OSError("[Errno 2] No such file: '/srv/secret/montaj.db'"),
        _invalid_record,
        MediaError("ffmpeg failed\n/srv/secret/video.mp4: invalid data"),
        MediaError("C:\\secret\\video.mp4: invalid data"),
        SourceError("/srv/secret/in.mp4: moov atom not found"),
        SpeechError("Cookie: secret signed URL https://x.example/secret", code=4),
    ],
)
def test_worker_errors_reach_the_panel_as_fixed_wording(
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    kind: str,
    error: BaseException | Callable[[], None],
) -> None:
    _add_source(db_path)
    jobs = JobQueue(Settings(data_dir=db_path.parent))
    held = _held_threads(monkeypatch)
    if isinstance(error, SourceError):
        error.__cause__ = MediaError(str(error))
    monkeypatch.setattr(_RUNNERS[kind], _raiser(error))
    _submit_both(jobs, kind)
    held[0].target(*held[0].args)
    activity = jobs.snapshot()
    assert activity is not None and not activity.running
    assert activity.summary.startswith("Stopped during")
    assert activity.problem and "secret" not in activity.problem
    assert "/" not in activity.problem and "\\" not in activity.problem
    assert jobs.running() is None
    assert caplog.records, "the detail is logged on the server"


@pytest.mark.parametrize("kind", ["batch", "stage"])
@pytest.mark.parametrize(
    "message",
    [
        "This source is already being processed by job since 2026-11-23T10:00:00+00:00.",
        "Rank candidates before processing clips.",
    ],
)
def test_worker_shows_safe_messages_as_written(
    db_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, message: str
) -> None:
    _add_source(db_path)
    jobs = JobQueue(Settings(data_dir=db_path.parent))
    held = _held_threads(monkeypatch)
    error = MediaError(message) if "processed" in message else ValueError(message)
    monkeypatch.setattr(_RUNNERS[kind], _raiser(error))
    _submit_both(jobs, kind)
    held[0].target(*held[0].args)
    activity = jobs.snapshot()
    assert activity is not None and activity.problem == message


@pytest.mark.parametrize("kind", ["batch", "stage"])
def test_worker_releases_the_slot_when_the_thread_exits(
    db_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    _add_source(db_path)
    jobs = JobQueue(Settings(data_dir=db_path.parent))
    held = _held_threads(monkeypatch)
    monkeypatch.setattr(_RUNNERS[kind], _raiser(SystemExit(3)))
    _submit_both(jobs, kind)
    assert jobs.running() is not None
    with pytest.raises(SystemExit):
        held[0].target(*held[0].args)
    activity = jobs.snapshot()
    assert activity is not None and not activity.running and activity.problem
    assert jobs.running() is None


def test_stage_submission_takes_no_budget_override() -> None:
    assert "budget" not in StageSubmission.model_fields


def test_run_spend_uses_the_ceiling_of_the_running_job(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_source(db_path)
    settings = Settings(data_dir=db_path.parent)
    jobs = JobQueue(settings)
    _held_threads(monkeypatch)
    jobs.submit(Submission(resume=7, budget=0.25, confirm=True))
    assert jobs.run_spend() == (0.0, 0.25)


def test_budget_override_above_the_run_ceiling_is_refused(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_source(db_path)
    settings = Settings(data_dir=db_path.parent)
    jobs = JobQueue(settings)
    held = _held_threads(monkeypatch)
    above = settings.max_cost_per_run_usd + 0.01
    with pytest.raises(ValueError, match=r"run ceiling of \$0\.50"):
        jobs.submit(Submission(resume=7, budget=above, confirm=True))
    assert held == [] and jobs.snapshot() is None
