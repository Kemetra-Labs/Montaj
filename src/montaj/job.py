"""Produce an English campaign batch through checks for human review."""

import sqlite3
import wave
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter
from typing import Annotated

import typer

from montaj.budget import Budget, BudgetExceeded, BudgetOverride
from montaj.candidates import CANDIDATE_LIMIT, CandidateBounds, find_candidates
from montaj.captions import caption_source, source_inputs
from montaj.check import check_source
from montaj.claude import LLM
from montaj.config import Settings
from montaj.cut import Renderer, cut_source
from montaj.db import connect, fetch
from montaj.draft import draft_source
from montaj.external import ExternalCallError
from montaj.ingest import Downloader, ingest_video, load_source
from montaj.media import MediaError, reuse_file_hash, source_lock
from montaj.models import Campaign, CampaignRules, CheckResult, Clip, ClipStatus, Niche
from montaj.pipeline import input_hash
from montaj.rank import has_review_decision, rank_source
from montaj.review import approval_problem
from montaj.transcribe import Transcriber, transcribe_source

DEFAULT_TARGET = 10
NO_MORE_CANDIDATES = "No more eligible candidates in the ranked batch."
_JOB_ERRORS = (
    ExternalCallError,
    MediaError,
    OSError,
    sqlite3.Error,
    ValueError,
    wave.Error,
)


@dataclass(slots=True)
class JobResult:
    source_id: int
    target: int
    ready: list[int] = field(default_factory=list)
    warnings: int = 0
    failures: int = 0
    screened: int = 0
    previously_approved: int = 0
    stop: str | None = None

    def summary(self) -> str:
        return (
            f"Source {self.source_id}: {len(self.ready)}/{self.target} clips ready or already approved "
            f"({self.warnings} awaiting warning confirmation, {self.previously_approved} already approved). "
            f"{self.failures} failed checks, {self.screened} screened before rendering."
        )


def _retain_approval(
    settings: Settings, directory: Path, clip: Clip, rules: CampaignRules
) -> bool:
    if clip.status not in (ClipStatus.APPROVED, ClipStatus.EXPORTED, ClipStatus.POSTED):
        return False
    if not clip.final_path:
        return False
    relative = Path(clip.final_path)
    if relative.is_absolute() or ".." in relative.parts:
        return False
    root = settings.data_dir.resolve()
    render = (root / relative).resolve()
    if not render.is_relative_to(root) or not render.is_file():
        return False
    inputs = source_inputs(
        settings, directory, rules_hash=input_hash(rules.compliance_payload())
    )
    return approval_problem(clip, inputs) is None


def _job_rules(settings: Settings, campaign_id: int | None) -> CampaignRules:
    with connect(settings.db_path, read_only=True) as db:
        campaign = fetch(db, Campaign, campaign_id) if campaign_id else None
    if campaign is None:
        raise ValueError("This source needs a campaign before it can run as a job.")
    return campaign.rules


def produce_batch(
    settings: Settings,
    source_id: int,
    *,
    top: int,
    budget: Budget,
    progress: Callable[[str], None] = print,
    backend: Transcriber | None = None,
    llm: LLM | None = None,
    renderer: Renderer | None = None,
) -> JobResult:
    if not 1 <= top <= CANDIDATE_LIMIT:
        raise ValueError(f"Choose a clip target between 1 and {CANDIDATE_LIMIT}.")
    _, _, directory = load_source(settings, source_id)
    result = JobResult(source_id, top)
    with (
        source_lock(directory, command="job"),
        reuse_file_hash(directory / "video.mp4"),
    ):
        source, metadata, _ = load_source(settings, source_id)
        if not (source.allowed_basis or "").strip():
            raise ValueError(
                "Record a permission basis on the source before creating a job."
            )
        rules = _job_rules(settings, source.campaign_id)
        bounds = CandidateBounds(
            min_seconds=rules.min_seconds, max_seconds=rules.max_seconds
        )
        try:
            progress("Transcribing English audio")
            transcribe_source(
                settings, source_id, language="en", budget=budget, backend=backend
            )
            progress("Finding candidates")
            find_candidates(settings, source_id, bounds=bounds, budget=budget, llm=llm)
            progress("Ranking candidates")
            ranking, _ = rank_source(
                settings,
                source_id,
                campaign_id=source.campaign_id,
                top=CANDIDATE_LIMIT,
                budget=budget,
                llm=llm,
            )
            result.screened = len(ranking.clips) - len(ranking.selected_ids)
            for clip_id in ranking.selected_ids:
                if len(result.ready) >= top:
                    break
                with connect(settings.db_path, read_only=True) as db:
                    clip = fetch(db, Clip, clip_id)
                if clip is None or clip.source_id != source_id:
                    raise ValueError(f"Ranked clip {clip_id} is missing. Rerun rank.")
                if has_review_decision(clip):
                    if _retain_approval(settings, directory, clip, rules):
                        result.ready.append(clip_id)
                        result.previously_approved += 1
                    continue
                if clip.end_s > metadata.duration + 0.05:
                    result.screened += 1
                    continue
                progress(f"Rendering clip {clip_id}")
                cut_source(settings, source_id, clip_id, renderer=renderer)
                caption_source(settings, source_id, clip_id)
                progress(f"Drafting clip {clip_id}")
                draft_source(
                    settings, source_id, clip_id=clip_id, budget=budget, llm=llm
                )
                progress(f"Checking clip {clip_id}")
                [(_, outcome)] = check_source(settings, source_id, clip_id=clip_id)
                if outcome.result == CheckResult.FAIL:
                    result.failures += 1
                    continue
                result.ready.append(clip_id)
                result.warnings += outcome.result == CheckResult.WARN
        except BudgetExceeded as exc:
            result.stop = str(exc)
    if len(result.ready) < top and result.stop is None:
        result.stop = NO_MORE_CANDIDATES
    return result


def start_job(
    settings: Settings,
    location: str,
    *,
    campaign: int,
    niche: Niche,
    allowed: bool,
    basis: str | None,
    downloader: Downloader | None = None,
) -> int:
    source_id, _ = ingest_video(
        settings,
        location,
        niche,
        allowed=allowed,
        basis=basis,
        campaign_id=campaign,
        downloader=downloader,
    )
    return source_id


def job(
    location: Annotated[str | None, typer.Argument()] = None,
    campaign: Annotated[int | None, typer.Option(min=1)] = None,
    niche: Annotated[Niche | None, typer.Option()] = None,
    allowed: Annotated[bool, typer.Option("--allowed")] = False,
    basis: Annotated[str | None, typer.Option()] = None,
    resume: Annotated[int | None, typer.Option(min=1)] = None,
    top: Annotated[int, typer.Option(min=1, max=CANDIDATE_LIMIT)] = DEFAULT_TARGET,
    budget: BudgetOverride = None,
) -> None:
    """Produce a campaign batch from a permitted English video."""
    started = perf_counter()
    source_id = resume
    try:
        settings = Settings.from_env()
        limits = Budget.from_settings(settings, budget)
        if resume is not None:
            if any((location, campaign, niche, allowed, basis)):
                raise ValueError(
                    "Use --resume with a source ID or submit a new source."
                )
        else:
            if not location or campaign is None or niche is None:
                raise ValueError("Provide a source location, --campaign and --niche.")
            typer.echo("Importing source")
            source_id = start_job(
                settings,
                location,
                campaign=campaign,
                niche=niche,
                allowed=allowed,
                basis=basis,
            )
        assert source_id is not None
        typer.echo(f"Source {source_id}")
        result = produce_batch(
            settings, source_id, top=top, budget=limits, progress=typer.echo
        )
    except _JOB_ERRORS as exc:
        typer.echo(f"Job stopped: {exc}", err=True)
        if source_id is not None:
            typer.echo(
                f"Resume with: montaj job --resume {source_id} --top {top}", err=True
            )
        raise typer.Exit(1) from exc
    typer.echo(result.summary())
    typer.echo(f"Elapsed: {perf_counter() - started:.2f}s")
    typer.echo(f"Preview: http://127.0.0.1:8765/?source={source_id}&ready=1")
    if result.stop:
        typer.echo(f"Batch shortfall: {result.stop}")
