"""Draft two hooks, a caption, and hashtags for each captioned clip."""

import json
import sqlite3
import tomllib
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from pydantic import BaseModel, Field, ValidationError, model_validator

from montaj.budget import Budget, BudgetExceeded, BudgetOverride
from montaj.candidates import parse_json_output
from montaj.captions import source_inputs
from montaj.claude import LLM, Claude
from montaj.config import Settings
from montaj.cut import selected_clips
from montaj.db import connect, fetch, save_clip
from montaj.external import ExternalCallError
from montaj.ingest import load_source
from montaj.media import MediaError, atomic_text, source_lock
from montaj.models import (
    Campaign,
    CampaignRules,
    Clip,
    ClipStage,
    ClipStatus,
    Niche,
)
from montaj.pipeline import input_hash, is_stale, stage_input_hash
from montaj.rank import clip_words
from montaj.transcribe import Transcript, require_usable_speech

_PROMPT = Path("prompts/draft.md")
_ACCOUNTS = Path("config/accounts.toml")
_HOOK_LIMIT = 12


class HookStyle(StrEnum):
    QUESTION = "question"
    BOLD_CLAIM = "bold_claim"
    STORY = "story"
    NUMBER = "number"
    CURIOSITY_GAP = "curiosity_gap"


class HookOption(BaseModel):
    text: str = Field(min_length=1)
    style: HookStyle

    @model_validator(mode="after")
    def limit_words(self) -> "HookOption":
        if len(self.text.split()) > _HOOK_LIMIT:
            raise ValueError(f"Hook is over {_HOOK_LIMIT} words.")
        return self


class DraftResult(BaseModel):
    hooks: list[HookOption] = Field(min_length=2, max_length=2)
    caption: str = Field(min_length=1)
    hashtags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_two_distinct_hooks(self) -> "DraftResult":
        first, second = self.hooks
        if first.style == second.style:
            raise ValueError("The two hooks must use different styles.")
        if " ".join(first.text.casefold().split()) == " ".join(
            second.text.casefold().split()
        ):
            raise ValueError("The two hooks must not repeat each other.")
        cleaned = []
        for tag in self.hashtags:
            text = tag.strip()
            if not text:
                raise ValueError("Hashtags must not be blank.")
            cleaned.append(text)
        self.hashtags = cleaned
        return self


def style_notes(niche: Niche, account_notes: list[str]) -> str:
    if not _ACCOUNTS.is_file():
        raise ValueError("Style notes are missing at config/accounts.toml.")
    with _ACCOUNTS.open("rb") as handle:
        table = tomllib.load(handle)
    section = table.get(niche.value)
    notes = section.get("style_notes") if isinstance(section, dict) else None
    if not isinstance(notes, str) or not notes.strip():
        raise ValueError(f"Add style notes for {niche.value} in config/accounts.toml.")
    extras = [note.strip() for note in account_notes if note.strip()]
    if not extras:
        return notes.strip()
    return notes.strip() + "\n" + "\n".join(extras)


def _request_draft(client: LLM, prompt: str) -> DraftResult:
    follow_up = (
        prompt + "\nReturn only the JSON object. Each hook must be 12 words or fewer."
    )
    for request in (prompt, follow_up):
        try:
            return DraftResult.model_validate_json(
                parse_json_output(client.complete(request))
            )
        except ValidationError:
            continue
    raise ValueError("Claude returned an invalid draft twice.")


def _apply(clip: Clip, draft: DraftResult, key: str) -> None:
    first, second = draft.hooks
    changed = (
        clip.hook_a != first.text
        or clip.hook_b != second.text
        or clip.caption != draft.caption
        or clip.hashtags != draft.hashtags
    )
    clip.hook_a = first.text
    clip.hook_a_style = first.style.value
    clip.hook_b = second.text
    clip.hook_b_style = second.style.value
    clip.caption = draft.caption
    clip.hashtags = draft.hashtags
    clip.input_hashes[ClipStage.DRAFT] = key
    if not changed and clip.chosen_hook is not None:
        return
    clip.chosen_hook = None
    clip.hook_text = None
    clip.hook_style = None
    if clip.status in (ClipStatus.APPROVED, ClipStatus.EXPORTED):
        clip.status = ClipStatus.DRAFTED
        clip.changed_since_review = True
    elif clip.status != ClipStatus.POSTED:
        clip.status = ClipStatus.DRAFTED


def draft_source(
    settings: Settings,
    source_id: int,
    *,
    clip_id: int | None,
    budget: Budget,
    llm: LLM | None = None,
    wait: float = 0,
) -> tuple[int, int]:
    source, _, directory = load_source(settings, source_id)
    require_usable_speech(source)
    if not _PROMPT.is_file():
        raise ValueError("Draft prompt is missing at prompts/draft.md.")
    template = _PROMPT.read_text(encoding="utf-8")
    with connect(settings.db_path) as db:
        campaign = (
            fetch(db, Campaign, source.campaign_id)
            if source.campaign_id is not None
            else None
        )
        notes = [
            row[0]
            for row in db.execute(
                "SELECT style_notes FROM accounts WHERE niche = ? AND style_notes IS NOT NULL",
                (source.niche,),
            )
        ]
    if source.campaign_id is not None and campaign is None:
        raise ValueError(f"Campaign {source.campaign_id} does not exist.")
    rules = (
        campaign.rules
        if campaign is not None
        else CampaignRules(min_seconds=20, max_seconds=60)
    )
    platform = campaign.platform.value if campaign is not None else None
    voice = style_notes(source.niche, notes)
    option = input_hash(
        template,
        rules.model_dump(mode="json"),
        voice,
        platform,
        settings.claude_model,
        "draft-v1",
    )
    drafted = 0
    with source_lock(directory, command="draft", wait=wait):
        clips = selected_clips(settings, source_id, directory, clip_id)
        transcript = Transcript.model_validate_json(
            (directory / "transcript.json").read_bytes()
        )
        inputs = source_inputs(
            settings,
            directory,
            rules_hash=input_hash(rules.model_dump(mode="json")),
            extra={ClipStage.DRAFT: option},
        )
        for clip in clips:
            if clip.id is None or not clip.final_path:
                raise ValueError(
                    f"Clip {clip.id} has no captioned render. Run montaj captions."
                )
            if is_stale(clip, ClipStage.CAPTIONS, inputs):
                raise ValueError(
                    f"Clip {clip.id} captions are out of date. Run montaj captions."
                )
            key = stage_input_hash(clip, ClipStage.DRAFT, inputs)
            if clip.input_hashes.get(ClipStage.DRAFT) == key and clip.hook_a:
                continue
            words = clip_words(transcript, clip.start_s, clip.end_s)
            text = " ".join(word.text for word in words)
            if not text:
                raise ValueError(
                    f"Clip {clip.id} has no transcript text to draft from."
                )
            payload = {
                "language": transcript.language,
                "moment_type": clip.moment_type,
                "transcript": text,
                "style_notes": voice,
                "platform": platform,
                "niche": source.niche.value,
                "rules": rules.model_dump(mode="json"),
            }
            prompt = (
                template
                + "\n\n"
                + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            )
            cache = directory / ".draft" / f"{clip.id}-{key}.json"
            cache.parent.mkdir(parents=True, exist_ok=True)
            if cache.is_file():
                try:
                    result = DraftResult.model_validate_json(cache.read_bytes())
                except ValidationError as exc:
                    raise ValueError(
                        f"Stored draft for clip {clip.id} is invalid. Delete {cache.name}."
                    ) from exc
            else:
                result = _request_draft(
                    llm or Claude(settings, source_id, "draft", budget), prompt
                )
                atomic_text(cache, result.model_dump_json())
            _apply(clip, result, key)
            with connect(settings.db_path) as db:
                save_clip(db, clip)
            drafted += 1
    return drafted, len(clips) - drafted


def draft(
    source_id: Annotated[int, typer.Argument(min=1)],
    clip: Annotated[int | None, typer.Option(min=1)] = None,
    budget: BudgetOverride = None,
    wait: Annotated[
        float,
        typer.Option("--wait", min=0, help="Seconds to wait if this source is locked."),
    ] = 0,
) -> None:
    """Write two hooks, a caption, and hashtags for captioned clips."""
    try:
        settings = Settings.from_env()
        drafted, reused = draft_source(
            settings,
            source_id,
            clip_id=clip,
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
        typer.echo(f"Draft failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"Source {source_id}: {drafted} drafted, {reused} reused.")
