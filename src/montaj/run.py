"""Run the paid and render stages in order. Review and export stay manual."""

import sqlite3
import wave
from collections.abc import Callable
from dataclasses import dataclass
from time import perf_counter
from typing import Annotated

import typer

from montaj.budget import Budget, BudgetExceeded, BudgetOverride, check_budget
from montaj.candidates import CandidateBounds, find_candidates
from montaj.captions import caption_source
from montaj.check import check_source
from montaj.claude import LLM
from montaj.config import Settings
from montaj.costs import (
    Service,
    Usage,
    cost_totals,
    deepgram_billing_model,
    estimate_cost,
)
from montaj.cut import Renderer, cut_source
from montaj.db import connect
from montaj.draft import draft_source
from montaj.ingest import load_source
from montaj.media import MediaError, source_lock
from montaj.rank import rank_source
from montaj.transcribe import (
    Transcriber,
    audio_duration,
    cached_transcript,
    transcribe_source,
    transcript_input_hashes,
)

STAGES = (
    "transcribe",
    "candidates",
    "rank",
    "cut",
    "captions",
    "draft",
    "check",
)


class RunError(RuntimeError):
    """A stage failed. Earlier stages are already saved, so a rerun resumes."""

    def __init__(self, stage: str, reason: str) -> None:
        self.stage = stage
        super().__init__(f"{stage}: {reason}")


@dataclass(frozen=True, slots=True)
class StageReport:
    stage: str
    detail: str
    seconds: float
    cost_usd: float

    def line(self) -> str:
        return (
            f"{self.stage}: {self.detail} in {self.seconds:.2f}s (${self.cost_usd:.6f})"
        )


def _spent(settings: Settings, source_id: int, stage: str) -> float:
    if not settings.db_path.is_file():
        return 0.0
    with connect(settings.db_path) as db:
        row = db.execute(
            """SELECT COALESCE(SUM(estimated_cost_usd), 0) FROM api_calls
               WHERE source_id = ? AND stage = ?""",
            (source_id, stage),
        ).fetchone()
    return float(row[0])


def _run_stage(
    settings: Settings, source_id: int, stage: str, action: Callable[[], str]
) -> StageReport:
    before = _spent(settings, source_id, stage)
    started = perf_counter()
    try:
        detail = action()
    except Exception as exc:
        raise RunError(stage, str(exc)) from exc
    return StageReport(
        stage,
        detail,
        perf_counter() - started,
        _spent(settings, source_id, stage) - before,
    )


def _planned_stages(until: str) -> tuple[str, ...]:
    if until not in STAGES:
        names = ", ".join(STAGES)
        raise ValueError(f"Unknown stage {until}. Choose one of: {names}.")
    return STAGES[: STAGES.index(until) + 1]


def estimate_run(
    settings: Settings, source_id: int, until: str, language: str, budget: Budget
) -> list[str]:
    """Price transcription offline and show prior spend for the downstream stages."""
    stages = _planned_stages(until)
    _, _, directory = load_source(settings, source_id, read_only=True)
    audio = directory / "audio.wav"
    keys = transcript_input_hashes(settings, audio, language)
    transcript = cached_transcript(directory, keys)
    transcription = 0.0
    if transcript is None:
        transcription = estimate_cost(
            Service.DEEPGRAM,
            deepgram_billing_model(settings.deepgram_model, language),
            Usage(seconds=audio_duration(audio), diarization=settings.deepgram_diarize),
        )
    with connect(settings.db_path, read_only=True) as db:
        past = cost_totals(db, source_id)
        lines = [
            f"Source {source_id} prior estimated API spend: ${past.total:.6f}",
            f"Run ceiling: ${budget.per_run:.6f} | Source ceiling: ${budget.per_source:.6f}",
        ]
        if transcript is not None:
            lines.append("transcribe: current transcript cached ($0.000000)")
        else:
            diarization = "on" if settings.deepgram_diarize else "off"
            lines.append(
                f"transcribe: ${transcription:.6f} for one full audio pass "
                f"(diarization {diarization}, before retries or chunk reuse)"
            )
            try:
                check_budget(db, budget, source_id, "transcribe", transcription)
            except BudgetExceeded as exc:
                lines.append(f"Full transcription pass exceeds available budget. {exc}")
            else:
                lines.append("Full transcription pass fits the current budget.")
    for stage in ("candidates", "rank", "draft"):
        if stage in stages:
            lines.append(
                f"{stage}: remaining cost depends on inputs and cache reuse "
                f"(prior estimated spend ${past.by_stage.get(stage, 0):.6f})"
            )
    return lines


def run_source(
    settings: Settings,
    source_id: int,
    *,
    campaign_id: int | None = None,
    until: str = "check",
    budget: Budget,
    language: str | None = None,
    force_language: bool = False,
    top: int = 10,
    min_seconds: float = 20,
    max_seconds: float = 60,
    backend: Transcriber | None = None,
    llm: LLM | None = None,
    renderer: Renderer | None = None,
    wait: float = 0,
) -> list[StageReport]:
    """Call each existing stage function, stopping at the first failure."""
    stages = _planned_stages(until)
    requested = language or settings.deepgram_language
    steps: dict[str, Callable[[], str]] = {
        "transcribe": lambda: _transcribe(
            settings, source_id, requested, budget, force_language, backend
        ),
        "candidates": lambda: _candidates(
            settings, source_id, budget, min_seconds, max_seconds, llm
        ),
        "rank": lambda: _rank(settings, source_id, campaign_id, top, budget, llm),
        "cut": lambda: _cut(settings, source_id, renderer),
        "captions": lambda: _captions(settings, source_id),
        "draft": lambda: _draft(settings, source_id, budget, llm),
        "check": lambda: _check(settings, source_id),
    }
    _, _, directory = load_source(settings, source_id)
    reports: list[StageReport] = []
    with source_lock(directory, command="run", wait=wait):
        for stage in stages:
            reports.append(_run_stage(settings, source_id, stage, steps[stage]))
    return reports


def _transcribe(
    settings: Settings,
    source_id: int,
    language: str,
    budget: Budget,
    force_language: bool,
    backend: Transcriber | None,
) -> str:
    transcript, reused = transcribe_source(
        settings,
        source_id,
        language=language,
        budget=budget,
        backend=backend,
        force_language=force_language,
    )
    verb = "reused" if reused else "transcribed"
    return f"{len(transcript.words)} words {verb}"


def _candidates(
    settings: Settings,
    source_id: int,
    budget: Budget,
    min_seconds: float,
    max_seconds: float,
    llm: LLM | None,
) -> str:
    result, reused = find_candidates(
        settings,
        source_id,
        bounds=CandidateBounds(min_seconds=min_seconds, max_seconds=max_seconds),
        budget=budget,
        llm=llm,
    )
    verb = "reused" if reused else "found"
    return f"{len(result.candidates)} candidates {verb}"


def _rank(
    settings: Settings,
    source_id: int,
    campaign_id: int | None,
    top: int,
    budget: Budget,
    llm: LLM | None,
) -> str:
    result, reused = rank_source(
        settings,
        source_id,
        campaign_id=campaign_id,
        top=top,
        budget=budget,
        llm=llm,
    )
    verb = "reused" if reused else "ranked"
    return f"{len(result.selected_ids)} selected {verb}"


def _cut(settings: Settings, source_id: int, renderer: Renderer | None) -> str:
    rendered, reused = cut_source(settings, source_id, renderer=renderer)
    return f"{rendered} rendered, {reused} reused"


def _captions(settings: Settings, source_id: int) -> str:
    rendered, reused = caption_source(settings, source_id)
    return f"{rendered} captioned, {reused} reused"


def _draft(settings: Settings, source_id: int, budget: Budget, llm: LLM | None) -> str:
    drafted, reused = draft_source(
        settings, source_id, clip_id=None, budget=budget, llm=llm
    )
    return f"{drafted} drafted, {reused} reused"


def _check(settings: Settings, source_id: int) -> str:
    outcomes = check_source(settings, source_id)
    return f"{len(outcomes)} checked"


def run(
    source_id: Annotated[int, typer.Argument(min=1)],
    campaign: Annotated[int | None, typer.Option(min=1)] = None,
    until: Annotated[str, typer.Option()] = "check",
    budget: BudgetOverride = None,
    estimate: Annotated[
        bool,
        typer.Option(
            "--estimate",
            help="Estimate transcription and show prior API spend offline.",
        ),
    ] = False,
    language: Annotated[
        str | None, typer.Option(help="Deepgram language code, such as en or ar-EG.")
    ] = None,
    force_language: Annotated[
        bool,
        typer.Option("--force-language", help="Continue when detection disagrees."),
    ] = False,
    top: Annotated[int, typer.Option(min=1)] = 10,
    wait: Annotated[
        float,
        typer.Option("--wait", min=0, help="Seconds to wait if this source is locked."),
    ] = 0,
) -> None:
    """Run transcribe through check. Does not review, export, or post."""
    try:
        settings = Settings.from_env()
        limits = Budget.from_settings(settings, budget)
        if estimate:
            for line in estimate_run(
                settings,
                source_id,
                until,
                language or settings.deepgram_language,
                limits,
            ):
                typer.echo(line)
            return
        reports = run_source(
            settings,
            source_id,
            campaign_id=campaign,
            until=until,
            budget=limits,
            language=language,
            force_language=force_language,
            top=top,
            wait=wait,
        )
    except (MediaError, OSError, sqlite3.Error, wave.Error) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    except RunError as exc:
        typer.echo(f"Run stopped at {exc}.", err=True)
        raise typer.Exit(1) from exc
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    for report in reports:
        typer.echo(report.line())
