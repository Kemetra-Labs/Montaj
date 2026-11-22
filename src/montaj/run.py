"""Run the paid and render stages in order. Review and export stay manual."""

from collections.abc import Callable
from dataclasses import dataclass
from time import perf_counter
from typing import Annotated

import typer

from montaj.budget import Budget, BudgetOverride
from montaj.candidates import CandidateBounds, find_candidates
from montaj.captions import caption_source
from montaj.check import check_source
from montaj.claude import LLM
from montaj.config import Settings
from montaj.cut import Renderer, cut_source
from montaj.db import connect
from montaj.draft import draft_source
from montaj.rank import rank_source
from montaj.transcribe import Transcriber, transcribe_source

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
) -> list[StageReport]:
    """Call each existing stage function, stopping at the first failure."""
    if until not in STAGES:
        names = ", ".join(STAGES)
        raise ValueError(f"Unknown stage {until}. Choose one of: {names}.")
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
    reports: list[StageReport] = []
    for stage in STAGES[: STAGES.index(until) + 1]:
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
    language: Annotated[
        str | None, typer.Option(help="Deepgram language code, such as en or ar-EG.")
    ] = None,
    force_language: Annotated[
        bool,
        typer.Option("--force-language", help="Continue when detection disagrees."),
    ] = False,
    top: Annotated[int, typer.Option(min=1)] = 10,
) -> None:
    """Run transcribe through check. Does not review, export, or post."""
    settings = Settings.from_env()
    try:
        reports = run_source(
            settings,
            source_id,
            campaign_id=campaign,
            until=until,
            budget=Budget.from_settings(settings, budget),
            language=language,
            force_language=force_language,
            top=top,
        )
    except RunError as exc:
        typer.echo(f"Run stopped at {exc}.", err=True)
        raise typer.Exit(1) from exc
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    for report in reports:
        typer.echo(report.line())
