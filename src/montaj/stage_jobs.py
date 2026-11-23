"""Run one pipeline stage for a source, for the production board."""

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from montaj.budget import Budget, BudgetExceeded
from montaj.candidates import CANDIDATE_LIMIT, CandidateBounds, find_candidates
from montaj.captions import caption_source
from montaj.check import check_source
from montaj.claude import LLM
from montaj.config import Settings
from montaj.cut import Renderer, cut_source
from montaj.db import connect, fetch
from montaj.draft import draft_source
from montaj.ingest import load_source
from montaj.job import _job_rules
from montaj.media import reuse_file_hash
from montaj.models import CheckResult, Clip, ClipStatus
from montaj.rank import has_review_decision, load_rank, rank_source
from montaj.transcribe import Transcriber, transcribe_source


class BoardStage(StrEnum):
    TRANSCRIBE = "transcribe"
    RANK = "rank"  # candidates, then rank
    RENDER = "render"  # cut, then captions
    DRAFT = "draft"  # draft, then check


PAID_STAGES = frozenset({BoardStage.TRANSCRIBE, BoardStage.RANK, BoardStage.DRAFT})


@dataclass(frozen=True, slots=True)
class StageResult:
    summary: str
    problem: str = ""


@dataclass(frozen=True, slots=True)
class _Run:
    """What one stage call needs, so each stage function takes a single value."""

    settings: Settings
    source_id: int
    directory: Path
    campaign_id: int | None
    top: int
    budget: Budget
    progress: Callable[[str], None]
    retry_failed: bool
    backend: Transcriber | None
    llm: LLM | None
    renderer: Renderer | None


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def _selected_clips(run: _Run) -> list[Clip]:
    """Clips this run may touch, taken from the saved ranking, never a fresh one."""
    if not (run.directory / "rank.json").is_file():
        raise ValueError("Run rank first.")
    ids = load_rank(run.directory).selected_ids
    if not run.retry_failed:
        ids = ids[: run.top]
    clips: list[Clip] = []
    with connect(run.settings.db_path, read_only=True) as db:
        for clip_id in ids:
            clip = fetch(db, Clip, clip_id)
            if clip is None or clip.source_id != run.source_id:
                raise ValueError(f"Ranked clip {clip_id} is missing. Rerun rank.")
            clips.append(clip)
    # A retry touches only failed clips. Otherwise human decisions are left alone.
    if run.retry_failed:
        return [c for c in clips if c.status == ClipStatus.NEEDS_FIX]
    return [c for c in clips if not has_review_decision(c)]


def _transcribe(run: _Run) -> StageResult:
    run.progress("Transcribing English audio")
    transcribe_source(
        run.settings,
        run.source_id,
        language="en",
        budget=run.budget,
        backend=run.backend,
    )
    return StageResult(f"Transcribed source {run.source_id}.")


def _rank(run: _Run) -> StageResult:
    rules = _job_rules(run.settings, run.campaign_id)
    bounds = CandidateBounds(
        min_seconds=rules.min_seconds, max_seconds=rules.max_seconds
    )
    run.progress("Finding candidates")
    find_candidates(
        run.settings, run.source_id, bounds=bounds, budget=run.budget, llm=run.llm
    )
    run.progress("Ranking candidates")
    ranking, _ = rank_source(
        run.settings,
        run.source_id,
        campaign_id=run.campaign_id,
        top=CANDIDATE_LIMIT,
        budget=run.budget,
        llm=run.llm,
    )
    return StageResult(
        f"Ranked {_plural(len(ranking.clips), 'candidate')} "
        f"and selected {len(ranking.selected_ids)}."
    )


def _render(run: _Run) -> StageResult:
    clips = _selected_clips(run)
    for clip in clips:
        clip_id = clip.id
        assert clip_id is not None
        run.progress(f"Rendering clip {clip_id}")
        cut_source(run.settings, run.source_id, clip_id, renderer=run.renderer)
        run.progress(f"Captioning clip {clip_id}")
        caption_source(run.settings, run.source_id, clip_id)
    if not clips:
        return StageResult("No clips to render.")
    return StageResult(f"Rendered and captioned {_plural(len(clips), 'clip')}.")


def _draft(run: _Run) -> StageResult:
    clips = _selected_clips(run)
    tally: dict[CheckResult, list[int]] = {result: [] for result in CheckResult}
    for clip in clips:
        clip_id = clip.id
        assert clip_id is not None
        run.progress(f"Drafting clip {clip_id}")
        draft_source(
            run.settings,
            run.source_id,
            clip_id=clip_id,
            budget=run.budget,
            llm=run.llm,
        )
        run.progress(f"Checking clip {clip_id}")
        [(_, outcome)] = check_source(run.settings, run.source_id, clip_id=clip_id)
        tally[outcome.result].append(clip_id)
    if not clips:
        return StageResult("No clips to draft.")
    passed, warned, failed = (
        tally[CheckResult.PASS],
        tally[CheckResult.WARN],
        tally[CheckResult.FAIL],
    )
    ids = ", ".join(str(clip_id) for clip_id in failed)
    return StageResult(
        f"Drafted and checked {_plural(len(clips), 'clip')}: "
        f"{len(passed)} passed, {_plural(len(warned), 'warning')}, "
        f"{len(failed)} failed" + (f" (clip {ids})." if failed else ".")
    )


_STAGES: dict[BoardStage, Callable[[_Run], StageResult]] = {
    BoardStage.TRANSCRIBE: _transcribe,
    BoardStage.RANK: _rank,
    BoardStage.RENDER: _render,
    BoardStage.DRAFT: _draft,
}


def run_board_stage(
    settings: Settings,
    source_id: int,
    stage: BoardStage,
    *,
    top: int,
    budget: Budget,
    progress: Callable[[str], None],
    retry_failed: bool = False,
    backend: Transcriber | None = None,
    llm: LLM | None = None,
    renderer: Renderer | None = None,
) -> StageResult:
    """Run one stage. Each underlying stage function takes its own source lock.

    A spending ceiling stops the stage and comes back as a problem. Every other
    failure propagates for the caller to report.
    """
    if not 1 <= top <= CANDIDATE_LIMIT:
        raise ValueError(f"Choose a clip target between 1 and {CANDIDATE_LIMIT}.")
    source, _, directory = load_source(settings, source_id)
    run = _Run(
        settings,
        source_id,
        directory,
        source.campaign_id,
        top,
        budget,
        progress,
        retry_failed,
        backend,
        llm,
        renderer,
    )
    try:
        with reuse_file_hash(directory / "video.mp4"):
            return _STAGES[stage](run)
    except BudgetExceeded as exc:
        return StageResult(f"Stopped during {stage.value}.", problem=str(exc))
