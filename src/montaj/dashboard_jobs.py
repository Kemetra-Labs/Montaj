"""Operate one background production job from the loopback dashboard."""

import re
from dataclasses import dataclass, field, replace
from html import escape
from pathlib import Path, PureWindowsPath
from secrets import compare_digest, token_urlsafe
from threading import Lock, Thread
from time import perf_counter
from urllib.parse import parse_qs, urlsplit

from pydantic import BaseModel, Field

from montaj.budget import Budget
from montaj.candidates import CANDIDATE_LIMIT
from montaj.config import Settings
from montaj.dashboard import render_dashboard_page
from montaj.db import connect
from montaj.job import DEFAULT_TARGET, produce_batch, start_job
from montaj.models import Niche

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
        budget = Budget.from_settings(self.settings, submission.budget)
        with self._lock:
            if self._activity is not None and self._activity.running:
                raise ValueError("A production job is already running.")
            activity = Activity(
                "Starting",
                source_id=submission.resume,
                run_id=budget.run_id,
                target=submission.top,
            )
            self._activity = activity
            try:
                Thread(
                    target=self._work, args=(activity, submission, budget), daemon=True
                ).start()
            except RuntimeError:
                self._activity = None
                raise

    def _work(self, activity: Activity, submission: Submission, budget: Budget) -> None:
        def progress(stage: str) -> None:
            with self._lock:
                activity.stage = stage

        try:
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
        except Exception as exc:
            with self._lock:
                activity.summary = f"Stopped during {activity.stage.lower()}."
                activity.problem = str(exc)
                activity.running = False
                activity.finished = perf_counter()
        else:
            with self._lock:
                activity.summary = result.summary()
                activity.problem = result.stop or ""
                activity.running = False
                activity.finished = perf_counter()

    def page(self, message: str = "") -> bytes:
        with connect(self.settings.db_path, read_only=True) as db:
            campaigns = db.execute(
                "SELECT id, name FROM campaigns ORDER BY name"
            ).fetchall()
            activity = self.snapshot()
            spend = (
                0.0
                if activity is None
                else db.execute(
                    "SELECT COALESCE(SUM(estimated_cost_usd), 0) FROM api_calls WHERE run_id = ?",
                    (activity.run_id,),
                ).fetchone()[0]
            )
        campaign_options = "".join(
            f'<option value="{row[0]}">{escape(row[1])}</option>' for row in campaigns
        )
        niche_options = "".join(
            f'<option value="{niche.value}">{niche.value.capitalize()}</option>'
            for niche in Niche
        )
        form = (
            f"""<form class="job-form" action="{JOBS_PATH}" method="post">
<input type="hidden" name="token" value="{self.token}">
<label>Video <input name="location" maxlength="2048" required placeholder="YouTube URL or inbox filename"></label>
<label>Campaign <select name="campaign" required>{campaign_options}</select></label>
<label>Niche <select name="niche" required>{niche_options}</select></label>
<label>Permission basis <input name="basis" maxlength="200" required></label>
<label class="inline-field"><input type="checkbox" name="allowed" value="true" required> I have permission to use this video</label>
{_limits_fields()}
<button class="button primary">Start job</button></form>"""
            if campaigns
            else "<p>Create a campaign with <code>montaj campaign add</code> to start a job.</p>"
        )
        status = ""
        if activity is not None:
            elapsed = (activity.finished or perf_counter()) - activity.started
            links = (
                f'<a href="{JOBS_PATH}">Refresh status</a>' if activity.running else ""
            )
            if activity.source_id is not None and not activity.running:
                links += f' <a href="/?source={activity.source_id}&amp;ready=1">Review clips</a>'
            status = (
                '<section class="panel job-panel" aria-label="Latest job">'
                f"<h2>{escape(activity.stage if activity.running else activity.summary)}</h2>"
                f"<p>Elapsed {elapsed:.0f} s · Estimated API spend ${spend:.6f}</p>"
                f"<p>{escape(activity.problem)}</p>{links}</section>"
            )
        if activity is not None and activity.running:
            form = "<p>The worker is preparing a batch. You can review other sources while it runs.</p>"
            resume = ""
        else:
            source = activity.source_id if activity is not None else None
            resume = f"""<section class="panel job-panel"><h2>Resume a source</h2>
<form class="job-form" action="{JOBS_PATH}" method="post">
<input type="hidden" name="token" value="{self.token}">
<label>Source ID <input name="resume" type="number" min="1" value="{source or ""}" required></label>
{_limits_fields(activity.target if activity else DEFAULT_TARGET)}<button class="button">Resume job</button></form></section>"""
        notice = (
            f'<p class="notice" role="status">{escape(message)}</p>' if message else ""
        )
        return render_dashboard_page(
            "Production jobs",
            f'<div class="heading"><h1>Production jobs</h1></div>{notice}{status}'
            f'<section class="panel job-panel"><h2>New English video</h2>{form}</section>{resume}',
        )


def _limits_fields(target: int = DEFAULT_TARGET) -> str:
    return (
        f'<label>Target clips <input name="top" type="number" min="1" max="{CANDIDATE_LIMIT}" '
        f'value="{target}" required></label>'
        '<label>Budget override (USD, optional) <input name="budget" type="number" min="0" step="0.01"></label>'
    )
