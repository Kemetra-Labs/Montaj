"""Rank all eligible candidates in one model request per source and campaign."""

import json
import re
import sqlite3
from pathlib import Path
from typing import Annotated

import typer
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    ValidationError,
)

from montaj.budget import Budget, BudgetExceeded, BudgetOverride
from montaj.candidates import CandidateSet, SavedPick, parse_json_output
from montaj.claude import LLM, Claude
from montaj.config import Settings
from montaj.db import connect, fetch, save_clip
from montaj.external import ExternalCallError
from montaj.ingest import load_source
from montaj.media import MediaError, atomic_text, source_lock
from montaj.models import Campaign, CampaignRules, Clip, ClipStatus
from montaj.pipeline import input_hash
from montaj.transcribe import Transcript, Word, require_usable_speech


class Grade(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    clip_id: int = Field(gt=0)
    score: float = Field(ge=1, le=10, allow_inf_nan=False)
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class RankedClip(BaseModel):
    clip_id: int
    score: float | None
    reason: str
    selected: bool


class RankSet(BaseModel):
    input_hash: str
    campaign_id: int | None = None
    clips: list[RankedClip]

    @property
    def selected_ids(self) -> list[int]:
        return [clip.clip_id for clip in self.clips if clip.selected]


_grades = TypeAdapter(list[Grade])
_RANK_MAX_TOKENS = 4096


def load_rank(directory: Path) -> RankSet:
    path = directory / "rank.json"
    if not path.is_file():
        raise ValueError("Rank candidates before processing clips.")
    return RankSet.model_validate_json(path.read_bytes())


def clip_words(transcript: Transcript, start: float, end: float) -> list[Word]:
    return [word for word in transcript.words if start < word.end and word.start < end]


def banned_term(text: str, rules: CampaignRules) -> str | None:
    folded = " ".join(text.casefold().split())
    for term in [*rules.banned_words, *rules.banned_topics]:
        pattern = r"(?<!\w)" + re.escape(" ".join(term.casefold().split())) + r"(?!\w)"
        if re.search(pattern, folded):
            return term
    return None


def _filtered(
    picks: list[SavedPick], transcript: Transcript, rules: CampaignRules
) -> tuple[list[dict[str, object]], dict[int, str]]:
    eligible: list[dict[str, object]] = []
    rejected = {}
    for pick in picks:
        text = " ".join(
            word.text for word in clip_words(transcript, pick.start, pick.end)
        )
        length = pick.end - pick.start
        if not rules.min_seconds - 1e-8 <= length <= rules.max_seconds + 1e-8:
            rejected[pick.clip_id] = "Outside campaign duration range"
        elif term := banned_term(text, rules):
            rejected[pick.clip_id] = f"Matches banned word or topic: {term}"
        elif not text:
            rejected[pick.clip_id] = "No transcript text in the candidate"
        else:
            eligible.append({"text": text, **pick.model_dump(mode="json")})
    return eligible, rejected


def _validated_grades(body: str | bytes, ids: set[int]) -> list[Grade]:
    try:
        grades = _grades.validate_json(body)
    except ValidationError as exc:
        errors = exc.errors(
            include_url=False, include_context=False, include_input=False
        )
        first = errors[0]
        if first["type"] == "json_invalid":
            detail = "invalid or incomplete JSON"
        elif first["type"] == "list_type":
            detail = "expected a JSON array of grades"
        elif first["loc"] and first["loc"][-1] in {"clip_id", "score", "reason"}:
            detail = f"invalid grade {first['loc'][-1]}"
        else:
            detail = "expected each grade to contain clip_id, score, and reason"
        raise ValueError(detail) from None
    returned = {grade.clip_id for grade in grades}
    if len(grades) != len(returned):
        raise ValueError("duplicate candidate IDs")
    if returned - ids:
        raise ValueError("unexpected candidate IDs")
    if ids - returned:
        raise ValueError("missing candidate IDs")
    return grades


def _ratings(client: LLM, prompt: str, ids: set[int]) -> list[Grade]:
    response = client.complete(prompt)
    if not isinstance(response, str):
        raise ValueError(
            "Claude rank: expected text containing a JSON array of grades."
        )
    try:
        return _validated_grades(parse_json_output(response), ids)
    except ValueError as exc:
        raise ValueError(f"Claude rank: {exc}. No grades were saved.") from None


def _store(settings: Settings, output: Path, result: RankSet) -> None:
    with connect(settings.db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        for rank in result.clips:
            clip = fetch(db, Clip, rank.clip_id)
            if clip is None:
                raise ValueError(
                    f"Candidate clip {rank.clip_id} disappeared. Rerun candidates."
                )
            if clip.reviewed_at is not None or clip.status in (
                ClipStatus.APPROVED,
                ClipStatus.EXPORTED,
                ClipStatus.POSTED,
            ):
                continue
            clip.score = rank.score
            clip.decision_reason = rank.reason
            if rank.selected:
                if (
                    clip.status in (ClipStatus.CANDIDATE, ClipStatus.REJECTED)
                    and clip.check_result is None
                ):
                    clip.status = ClipStatus.RANKED
            else:
                clip.status = ClipStatus.REJECTED
            save_clip(db, clip)
        atomic_text(output, result.model_dump_json(indent=2) + "\n")


def rank_source(
    settings: Settings,
    source_id: int,
    *,
    campaign_id: int | None,
    top: int,
    budget: Budget,
    llm: LLM | None = None,
    wait: float = 0,
) -> tuple[RankSet, bool]:
    if top < 1:
        raise ValueError("Select at least one clip with --top.")
    source, _, directory = load_source(settings, source_id)
    require_usable_speech(source)
    campaign_id = campaign_id or source.campaign_id
    with connect(settings.db_path) as db:
        campaign = fetch(db, Campaign, campaign_id) if campaign_id is not None else None
    if campaign_id is not None and campaign is None:
        raise ValueError(f"Campaign {campaign_id} does not exist.")
    rules = (
        campaign.rules if campaign else CampaignRules(min_seconds=20, max_seconds=60)
    )
    template = Path("prompts/rank.md").read_text(encoding="utf-8")
    output = directory / "rank.json"
    with source_lock(directory, command="rank", wait=wait):
        candidates = CandidateSet.model_validate_json(
            (directory / "candidates.json").read_bytes()
        )
        transcript = Transcript.model_validate_json(
            (directory / "transcript.json").read_bytes()
        )
        eligible, rejected = _filtered(candidates.candidates, transcript, rules)
        key = input_hash(
            template,
            campaign_id,
            rules.model_dump(mode="json"),
            eligible,
            rejected,
            settings.claude_model,
            "rank-v1",
        )
        selection_hash = input_hash(key, top)
        if output.is_file():
            try:
                prior = load_rank(directory)
            except ValidationError:
                prior = None
            if prior is not None and prior.input_hash == selection_hash:
                return prior, True
        paid_cache = directory / f".rank-{key}.json"
        grades: list[Grade] = []
        if eligible:
            ids = {
                item.clip_id
                for item in candidates.candidates
                if item.clip_id not in rejected
            }
            if paid_cache.is_file():
                try:
                    grades = _validated_grades(paid_cache.read_bytes(), ids)
                except ValueError as exc:
                    raise ValueError(
                        f"Saved rank grade cache is invalid ({exc}). Move {paid_cache.name} aside before retrying."
                    ) from None
            else:
                payload = {
                    "campaign": rules.model_dump(mode="json"),
                    "candidates": eligible,
                }
                prompt = (
                    template
                    + "\n\n"
                    + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                )
                grades = _ratings(
                    llm
                    or Claude(
                        settings,
                        source_id,
                        "rank",
                        budget,
                        max_tokens=_RANK_MAX_TOKENS,
                        require_complete=True,
                        thinking="between_tools"
                        if settings.claude_model == "claude-sonnet-5-5"
                        else None,
                    ),
                    prompt,
                    ids,
                )
                atomic_text(paid_cache, _grades.dump_json(grades).decode("utf-8"))
        grades.sort(key=lambda item: (-item.score, item.clip_id))
        results = [
            RankedClip(
                clip_id=grade.clip_id,
                score=grade.score,
                reason=grade.reason
                if index < top
                else f"Below top {top}: {grade.reason}",
                selected=index < top,
            )
            for index, grade in enumerate(grades)
        ]
        results.extend(
            RankedClip(clip_id=clip_id, score=None, reason=reason, selected=False)
            for clip_id, reason in rejected.items()
        )
        result = RankSet(
            input_hash=selection_hash, campaign_id=campaign_id, clips=results
        )
        _store(settings, output, result)
    return result, False


def rank(
    source_id: Annotated[int, typer.Argument(min=1)],
    campaign: Annotated[int | None, typer.Option(min=1)] = None,
    top: Annotated[int, typer.Option(min=1)] = 10,
    budget: BudgetOverride = None,
    wait: Annotated[
        float,
        typer.Option("--wait", min=0, help="Seconds to wait if this source is locked."),
    ] = 0,
) -> None:
    """Rank candidate clips using campaign rules."""
    try:
        settings = Settings.from_env()
        result, reused = rank_source(
            settings,
            source_id,
            campaign_id=campaign,
            top=top,
            budget=Budget.from_settings(settings, budget),
            wait=wait,
        )
    except (
        BudgetExceeded,
        ExternalCallError,
        MediaError,
        OSError,
        ValueError,
        sqlite3.Error,
    ) as exc:
        typer.echo(f"Rank failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(
        f"Source {source_id}: {len(result.selected_ids)} selected{' (reused)' if reused else ''}."
    )
