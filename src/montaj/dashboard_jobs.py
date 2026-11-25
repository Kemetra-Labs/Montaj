"""Operate one background production job from the loopback dashboard."""

import logging
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path, PureWindowsPath
from secrets import compare_digest, token_urlsafe
from threading import Lock, Thread
from time import perf_counter
from typing import Literal
from urllib.parse import parse_qs, urlsplit

from pydantic import BaseModel, Field, ValidationError

from montaj.budget import Budget
from montaj.candidates import CANDIDATE_LIMIT
from montaj.config import Settings
from montaj.dashboard_parts import LiveJob, permission_reason, source_problem
from montaj.db import connect, fetch
from montaj.job import _JOB_ERRORS, DEFAULT_TARGET, produce_batch, start_job
from montaj.media import MediaError
from montaj.models import Niche, Source
from montaj.run import estimate_run
from montaj.stage_jobs import PAID_STAGES, BoardStage, run_board_stage
from montaj.transcribe import SpeechError

_LOG = logging.getLogger(__name__)
JOBS_PATH = "/dashboard/jobs"
FORM_TYPE = "application/x-www-form-urlencoded"
_YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}
_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")


class Submission(BaseModel):
    location: str = ""
    campaign: int | None = Field(default=None, gt=0)
    niche: Niche | None = None
    basis: str = ""
    allowed: bool = False
    resume: int | None = Field(default=None, gt=0)
    top: int = Field(default=DEFAULT_TARGET, ge=1, le=CANDIDATE_LIMIT)
    budget: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    confirm: bool = False


class StageSubmission(BaseModel):
    source: int = Field(gt=0)
    stage: BoardStage
    top: int = Field(default=DEFAULT_TARGET, ge=1, le=CANDIDATE_LIMIT)
    confirm: bool = False
    retry: bool = False


# The pipeline stage whose estimate covers each paid board stage.
_ESTIMATE_UNTIL = {
    BoardStage.TRANSCRIBE: "transcribe",
    BoardStage.RANK: "rank",
    BoardStage.DRAFT: "draft",
}
# Estimate lines shown for every stage, then the lines each stage owns.
_SHARED_LINES = ("Source ", "Run ceiling:")
_STAGE_LINES = {
    BoardStage.TRANSCRIBE: ("transcribe:", "Full transcription pass"),
    BoardStage.RANK: ("candidates:", "rank:"),
    BoardStage.DRAFT: ("draft:",),
}
# Stages without an offline price, named as the cost note reads.
_UNPRICED = {BoardStage.RANK: "candidates and rank", BoardStage.DRAFT: "draft"}
_CONFIRM_FIRST = "Confirm the estimated cost first."
_UNEXPECTED = (
    "Unexpected error. Check montaj.log in the logs folder of the data "
    "directory, then try again."
)
_FILE_PROBLEM = (
    "A saved file or record could not be read. Check the data directory, "
    "then try again."
)
_MEDIA_PROBLEM = "A media tool failed. Check the source media, then try again."
_PATH_SEPARATORS = ("/", "\\")


def _safe_first_line(text: str) -> str:
    """The first line of a message, or nothing when it names a file path."""
    lines = text.strip().splitlines()
    first = lines[0].strip() if lines else ""
    return "" if any(sep in first for sep in _PATH_SEPARATORS) else first[:300]


def _job_problem(exc: Exception) -> str:
    """What the live panel shows for a failed job. Every failure is logged in full,
    so paths and raw notes stay in the log."""
    if isinstance(exc, MediaError) or isinstance(exc.__cause__, MediaError):
        problem = _safe_first_line(str(exc)) or _MEDIA_PROBLEM
    elif isinstance(exc, OSError | ValidationError):
        problem = _FILE_PROBLEM
    elif isinstance(exc, SpeechError) and exc.code == 4:  # holds the raw failure note
        problem = source_problem("failed", None) or _UNEXPECTED
    elif isinstance(exc, _JOB_ERRORS):
        problem = str(exc)
    else:
        problem = _UNEXPECTED
    _LOG.error("Dashboard job stopped: %s", problem, exc_info=exc)
    return problem


def _caps_line(stages: str, budget: Budget) -> str:
    return (
        f"No offline estimate for {stages}; spend stops at the run ceiling "
        f"${budget.per_run:.2f} and the source ceiling ${budget.per_source:.2f}."
    )


def dashboard_location(settings: Settings, supplied: str) -> str:
    location = supplied.strip()
    if not location:
        raise ValueError("Enter a YouTube URL or inbox filename.")
    if "://" in location:
        url = urlsplit(location)
        if url.path == "/watch" and url.hostname != "youtu.be":
            ids = parse_qs(url.query, max_num_fields=24).get("v", [])
            video_id = ids[0] if len(ids) == 1 else ""
        elif url.hostname == "youtu.be":
            video_id = url.path.removeprefix("/")
        elif url.path.startswith(("/shorts/", "/live/", "/embed/")):
            video_id = url.path.rsplit("/", 1)[-1]
        else:
            video_id = ""
        if (
            url.scheme != "https"
            or url.hostname not in _YOUTUBE_HOSTS
            or url.username is not None
            or url.port not in (None, 443)
            or not _VIDEO_ID.fullmatch(video_id)
        ):
            raise ValueError("Enter an individual YouTube video URL or use the inbox.")
        return location
    relative = Path(location)
    windows = PureWindowsPath(location)
    if relative.anchor or windows.anchor or ".." in windows.parts:
        raise ValueError("Use a filename relative to the Montaj inbox.")
    inbox = (settings.data_dir / "inbox").resolve()
    path = (inbox / relative).resolve()
    if not path.is_relative_to(inbox) or not path.is_file():
        raise ValueError("Use an existing file inside the Montaj inbox.")
    return str(path)


@dataclass(slots=True)
class Activity:
    stage: str
    running: bool = True
    source_id: int | None = None
    summary: str = ""
    problem: str = ""
    run_id: str = ""
    target: int = DEFAULT_TARGET
    started: float = field(default_factory=perf_counter)
    finished: float | None = None
    kind: Literal["batch", "stage"] = "batch"
    stage_key: str = ""
    budget: Budget | None = None


class JobQueue:
    """A dashboard allows one background worker and keeps its latest result."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.token = token_urlsafe(32)
        self._lock = Lock()
        self._activity: Activity | None = None

    def accepts_token(self, token: str) -> bool:
        return token.isascii() and compare_digest(token, self.token)

    def snapshot(self) -> Activity | None:
        with self._lock:
            return replace(self._activity) if self._activity is not None else None

    def submit(self, submission: Submission) -> None:
        if not submission.confirm:
            raise ValueError(_CONFIRM_FIRST)
        if submission.resume is None:
            if not submission.allowed or not submission.basis.strip():
                raise ValueError("Confirm permission and record its basis.")
            if submission.campaign is None or submission.niche is None:
                raise ValueError("Choose a campaign and niche.")
            submission = submission.model_copy(
                update={
                    "location": dashboard_location(self.settings, submission.location)
                }
            )
        else:
            self._require_permitted(submission.resume)
        budget = self._budget(submission.budget)
        with self._lock:
            if self._activity is not None and self._activity.running:
                raise ValueError("A production job is already running.")
            activity = Activity(
                "Starting",
                source_id=submission.resume,
                run_id=budget.run_id,
                target=submission.top,
                budget=budget,
            )
            self._activity = activity
            try:
                Thread(
                    target=self._work, args=(activity, submission, budget), daemon=True
                ).start()
            except RuntimeError:
                self._activity = None
                raise

    def _budget(self, override: float | None) -> Budget:
        """The job's ceilings. An override may lower the run ceiling, never raise it."""
        ceiling = self.settings.max_cost_per_run_usd
        if override is not None and override > ceiling:
            raise ValueError(
                "Set a budget override no higher than the run ceiling "
                f"of ${ceiling:.2f}."
            )
        return Budget.from_settings(self.settings, override)

    def _progress(self, activity: Activity) -> Callable[[str], None]:
        def progress(stage: str) -> None:
            with self._lock:
                activity.stage = stage

        return progress

    def _guarded(
        self,
        activity: Activity,
        stopped: Callable[[], str],
        work: Callable[[], tuple[str, str]],
    ) -> None:
        """Run a job and always free the slot, even when the thread is torn down."""
        try:
            summary, problem = work()
        except Exception as exc:
            self._end(activity, f"Stopped during {stopped()}.", _job_problem(exc))
        except BaseException:
            self._end(activity, f"Stopped during {stopped()}.", _UNEXPECTED)
            raise
        else:
            self._end(activity, summary, problem)

    def _work(self, activity: Activity, submission: Submission, budget: Budget) -> None:
        self._guarded(
            activity,
            lambda: activity.stage.lower(),
            lambda: self._run_batch(activity, submission, budget),
        )

    def _run_batch(
        self, activity: Activity, submission: Submission, budget: Budget
    ) -> tuple[str, str]:
        progress = self._progress(activity)
        source_id = submission.resume
        if source_id is None:
            if submission.campaign is None or submission.niche is None:
                raise ValueError("Choose a campaign and niche.")
            progress("Importing permitted video")
            source_id = start_job(
                self.settings,
                submission.location,
                campaign=submission.campaign,
                niche=submission.niche,
                allowed=submission.allowed,
                basis=submission.basis,
            )
            with self._lock:
                activity.source_id = source_id
        result = produce_batch(
            self.settings,
            source_id,
            top=submission.top,
            budget=budget,
            progress=progress,
        )
        return result.summary(), result.stop or ""

    def _end(self, activity: Activity, summary: str, problem: str) -> None:
        with self._lock:
            activity.summary = summary
            activity.problem = problem
            activity.running = False
            activity.finished = perf_counter()

    def estimate(
        self, source_id: int, stage: BoardStage, budget: float | None
    ) -> list[str]:
        """Cost lines for one paid stage only. A free stage has nothing to estimate."""
        until = _ESTIMATE_UNTIL.get(stage)
        if until is None:
            return []
        caps = Budget.from_settings(self.settings, budget)
        lines = estimate_run(self.settings, source_id, until, "en", caps)
        owned = _SHARED_LINES + _STAGE_LINES[stage]
        kept = [line for line in lines if line.startswith(owned)]
        if stage in _UNPRICED:
            kept.append(_caps_line(_UNPRICED[stage], caps))
        return kept

    def estimate_batch(self, source_id: int) -> list[str]:
        """Cost lines for a Resume, which runs every stage through draft."""
        caps = Budget.from_settings(self.settings, None)
        lines = estimate_run(self.settings, source_id, "draft", "en", caps)
        return [*lines, _caps_line("candidates, rank and draft", caps)]

    def running(self) -> tuple[int, str] | None:
        """The source and strip key of the running job. Source 0 means an import."""
        with self._lock:
            activity = self._activity
            if activity is None or not activity.running:
                return None
            key = activity.stage_key if activity.kind == "stage" else "batch"
            return (activity.source_id or 0, key)

    def run_spend(self) -> tuple[float, float] | None:
        """Estimated spend of the live run and the per-run ceiling. None when idle."""
        activity = self.snapshot()
        if activity is None or not activity.running:
            return None
        with connect(self.settings.db_path, read_only=True) as db:
            spent = _run_spend(db, activity.run_id)
        budget = activity.budget or Budget.from_settings(self.settings, None)
        return spent, budget.per_run

    def live_job(self) -> LiveJob | None:
        """The current or latest job and its run spend. None before any job."""
        activity = self.snapshot()
        if activity is None:
            return None
        with connect(self.settings.db_path, read_only=True) as db:
            spent = _run_spend(db, activity.run_id)
        return LiveJob(
            activity.source_id,
            activity.stage,
            activity.running,
            (activity.finished or perf_counter()) - activity.started,
            spent,
            activity.summary,
            activity.problem,
        )

    def submit_stage(self, submission: StageSubmission) -> None:
        self._require_permitted(submission.source)
        budget = Budget.from_settings(self.settings, None)
        with self._lock:
            if self._activity is not None and self._activity.running:
                raise ValueError("A production job is already running.")
            if submission.stage in PAID_STAGES and not submission.confirm:
                raise ValueError(_CONFIRM_FIRST)
            activity = Activity(
                "Starting",
                source_id=submission.source,
                run_id=budget.run_id,
                target=submission.top,
                kind="stage",
                stage_key=submission.stage.value,
                budget=budget,
            )
            self._activity = activity
            try:
                Thread(
                    target=self._work_stage,
                    args=(activity, submission, budget),
                    daemon=True,
                ).start()
            except RuntimeError:
                self._activity = None
                raise

    def _require_permitted(self, source_id: int) -> None:
        with connect(self.settings.db_path, read_only=True) as db:
            source = fetch(db, Source, source_id)
        if source is None:
            raise ValueError(f"Source {source_id} was not found.")
        if permission_reason(source.source_allowed, source.allowed_basis):
            raise ValueError(
                "Only a source with recorded permission and its basis can run."
            )

    def _work_stage(
        self, activity: Activity, submission: StageSubmission, budget: Budget
    ) -> None:
        def work() -> tuple[str, str]:
            result = run_board_stage(
                self.settings,
                submission.source,
                submission.stage,
                top=submission.top,
                budget=budget,
                progress=self._progress(activity),
                retry_failed=submission.retry,
            )
            return result.summary, result.problem

        self._guarded(activity, lambda: submission.stage.value, work)


def _run_spend(db: sqlite3.Connection, run_id: str) -> float:
    row = db.execute(
        "SELECT COALESCE(SUM(estimated_cost_usd), 0) FROM api_calls WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    return float(row[0])
