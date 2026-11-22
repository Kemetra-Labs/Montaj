"""Find reusable, word-aligned clips from transcript windows."""

import json
import sqlite3
from bisect import bisect_left
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Self

import typer
from pydantic import BaseModel, Field, TypeAdapter, ValidationError, model_validator

from montaj.budget import Budget, BudgetExceeded, BudgetOverride
from montaj.claude import LLM, Claude
from montaj.config import Settings
from montaj.db import connect, fetch, insert
from montaj.external import ExternalCallError
from montaj.ingest import load_source
from montaj.media import MediaError, atomic_text, source_lock
from montaj.models import Clip
from montaj.pipeline import input_hash
from montaj.transcribe import Transcript, Word

WINDOW_SECONDS = 450
STEP_SECONDS = 390
SENTENCE_END = (".", "?", "!", "؟", "。", "！", "？", "…")


class MomentType(StrEnum):
    BOLD_CLAIM = "bold_claim"
    STORY = "story"
    SURPRISING_FACT = "surprising_fact"
    EMOTIONAL = "emotional"
    FUNNY = "funny"
    USEFUL_TIP = "useful_tip"
    CONFLICT = "conflict"


class Pick(BaseModel):
    start: float = Field(ge=0, allow_inf_nan=False)
    end: float = Field(gt=0, allow_inf_nan=False)
    moment_type: MomentType
    reason: str = Field(min_length=1)
    hook_idea: str = Field(min_length=1)

    @model_validator(mode="after")
    def ordered_bounds(self) -> Self:
        if self.end <= self.start:
            raise ValueError("Candidate end must follow its start.")
        return self


class SavedPick(Pick):
    clip_id: int


class CandidateSet(BaseModel):
    input_hash: str
    candidates: list[SavedPick]


class CandidateBounds(BaseModel):
    min_seconds: float = Field(default=20, gt=0, allow_inf_nan=False)
    max_seconds: float = Field(default=60, gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def ordered_bounds(self) -> Self:
        if self.max_seconds < self.min_seconds:
            raise ValueError("max_seconds must be at least min_seconds.")
        return self


_picks = TypeAdapter(list[Pick])


def windows(words: list[Word]) -> list[list[Word]]:
    if not words:
        return []
    starts = [word.start for word in words]
    result = []
    offset = starts[0]
    while offset <= starts[-1]:
        left = bisect_left(starts, offset)
        right = bisect_left(starts, offset + WINDOW_SECONDS)
        if right > left:
            result.append(words[left:right])
        if right == len(words):
            break
        offset += STEP_SECONDS
    return result


def snap_pick(pick: Pick, words: list[Word], bounds: CandidateBounds) -> Pick | None:
    if (
        not words
        or pick.start < words[0].start - 1
        or pick.end > max(word.end for word in words) + 1
    ):
        return None
    start = min(words, key=lambda word: abs(word.start - pick.start)).start
    eligible = [
        word
        for word in words
        if bounds.min_seconds - 1e-8 <= word.end - start <= bounds.max_seconds + 1e-8
    ]
    endings = [
        word
        for word in eligible
        if word.text.rstrip("\"'”’)]}").endswith(SENTENCE_END)
        and abs(word.end - pick.end) <= 5
    ]
    if not eligible:
        return None
    end = min(endings or eligible, key=lambda word: abs(word.end - pick.end)).end
    return pick.model_copy(update={"start": start, "end": end})


def unique_picks(picks: list[Pick], limit: int = 30) -> list[Pick]:
    selected: list[Pick] = []
    for candidate in picks:
        duplicate = any(
            min(candidate.end, other.end) - max(candidate.start, other.start)
            >= 0.5 * min(candidate.end - candidate.start, other.end - other.start)
            for other in selected
        )
        if not duplicate:
            selected.append(candidate)
        if len(selected) >= limit:
            break
    return sorted(selected, key=lambda pick: pick.start)


def parse_json_output(response: str) -> str:
    body = response.strip()
    fence = chr(96) * 3
    if body.startswith(fence) and body.endswith(fence):
        _, _, body = body.partition("\n")
        return body[:-3].strip()
    return body


def _model_picks(client: LLM, prompt: str) -> list[Pick]:
    try:
        return _picks.validate_json(parse_json_output(client.complete(prompt)))
    except ValidationError:
        follow_up = prompt + "\nReturn only the JSON array with the specified fields."
    try:
        return _picks.validate_json(parse_json_output(client.complete(follow_up)))
    except ValidationError:
        raise ValueError("Claude returned invalid candidate JSON twice.") from None


def _window_picks(
    client: LLM,
    template: str,
    transcript: Transcript,
    bounds: CandidateBounds,
    cache: Path,
) -> list[Pick]:
    words = sorted(transcript.words, key=lambda word: word.start)
    candidates = []
    for index, block in enumerate(windows(words)):
        path = cache / f"{index}.json"
        try:
            raw = _picks.validate_json(path.read_bytes())
        except (OSError, ValidationError):
            payload = {
                "language": transcript.language,
                "min_seconds": bounds.min_seconds,
                "max_seconds": bounds.max_seconds,
                "words": [[word.start, word.end, word.text] for word in block],
            }
            prompt = (
                template
                + "\n\n"
                + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            )
            raw = _model_picks(client, prompt)
            atomic_text(path, _picks.dump_json(raw).decode("utf-8"))
        for candidate in raw:
            if (snapped := snap_pick(candidate, block, bounds)) is not None:
                candidates.append(snapped)
    return unique_picks(candidates)


def _store(
    settings: Settings, source_id: int, picks: list[Pick], key: str, output: Path
) -> CandidateSet:
    with connect(settings.db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        rows = db.execute(
            "SELECT id, start_s, end_s, moment_type FROM clips WHERE source_id = ? ORDER BY id",
            (source_id,),
        )
        unique = {
            (row["start_s"], row["end_s"], row["moment_type"]): row["id"]
            for row in rows
        }
        saved = []
        for pick in picks:
            clip_id = unique.get((pick.start, pick.end, pick.moment_type))
            if clip_id is None:
                clip_id = insert(
                    db,
                    Clip(
                        source_id=source_id,
                        start_s=pick.start,
                        end_s=pick.end,
                        moment_type=pick.moment_type,
                    ),
                ).id
            if clip_id is None:
                raise ValueError("Could not assign a candidate clip ID.")
            saved.append(SavedPick(clip_id=clip_id, **pick.model_dump()))
        result = CandidateSet(input_hash=key, candidates=saved)
        atomic_text(output, result.model_dump_json(indent=2) + "\n")
    return result


def find_candidates(
    settings: Settings,
    source_id: int,
    *,
    bounds: CandidateBounds,
    budget: Budget,
    llm: LLM | None = None,
) -> tuple[CandidateSet, bool]:
    _, _, directory = load_source(settings, source_id)
    transcript_file = directory / "transcript.json"
    if not transcript_file.is_file():
        raise ValueError(f"Transcribe source {source_id} before finding candidates.")
    template = Path("prompts/candidates.md").read_text(encoding="utf-8")
    client = llm or Claude(settings, source_id, "candidates", budget)
    output = directory / "candidates.json"
    with source_lock(directory):
        transcript = Transcript.model_validate_json(transcript_file.read_bytes())
        key = input_hash(
            transcript.model_dump(mode="json"),
            template,
            bounds.model_dump(),
            settings.claude_model,
            "candidates-v1",
        )
        try:
            cached = CandidateSet.model_validate_json(output.read_bytes())
        except (OSError, ValidationError):
            cached = None
        if cached is not None and cached.input_hash == key:
            with connect(settings.db_path) as db:
                if all(
                    (clip := fetch(db, Clip, item.clip_id)) is not None
                    and clip.source_id == source_id
                    for item in cached.candidates
                ):
                    return cached, True
        windows_cache = directory / ".candidate-windows" / key
        windows_cache.mkdir(parents=True, exist_ok=True)
        picks = _window_picks(client, template, transcript, bounds, windows_cache)
        return _store(settings, source_id, picks, key, output), False


def candidates(
    source_id: Annotated[int, typer.Argument(min=1)],
    min_seconds: Annotated[float, typer.Option(min=0)] = 20,
    max_seconds: Annotated[float, typer.Option(min=0)] = 60,
    budget: BudgetOverride = None,
) -> None:
    """Find word-aligned candidate moments in a source transcript."""
    try:
        settings = Settings.from_env()
        result, reused = find_candidates(
            settings,
            source_id,
            bounds=CandidateBounds(min_seconds=min_seconds, max_seconds=max_seconds),
            budget=Budget.from_settings(settings, budget),
        )
    except (
        BudgetExceeded,
        ExternalCallError,
        MediaError,
        OSError,
        ValueError,
        sqlite3.Error,
    ) as exc:
        typer.echo(f"Candidates failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(
        f"Source {source_id}: {len(result.candidates)} candidates {'reused' if reused else 'found'}."
    )
