"""Terminal review: approve, reject, trim, or edit one clip at a time."""

import sqlite3
from collections.abc import Callable
from typing import Annotated

import typer

from montaj.captions import caption_source, source_inputs
from montaj.check import check_source
from montaj.config import Settings
from montaj.cut import cut_source, selected_clips
from montaj.db import connect, fetch, save_clip
from montaj.ingest import load_source
from montaj.media import MediaError
from montaj.models import (
    Account,
    Campaign,
    CampaignRules,
    CheckResult,
    Clip,
    ClipStage,
    ClipStatus,
    utc_now,
)
from montaj.pipeline import ClipInputs, input_hash, stage_input_hash
from montaj.rank import clip_words
from montaj.transcribe import Transcript

Reader = Callable[[str], str]
Writer = Callable[[str], None]

_DONE = {
    ClipStatus.APPROVED,
    ClipStatus.EXPORTED,
    ClipStatus.POSTED,
    ClipStatus.REJECTED,
}


def _line(reader: Reader, prompt: str) -> str:
    return reader(prompt).strip()


def _waiting(settings: Settings, source_id: int, skipped: set[int]) -> list[Clip]:
    _, _, directory = load_source(settings, source_id)
    clips = selected_clips(settings, source_id, directory)
    return [
        clip
        for clip in clips
        if clip.id not in skipped
        and clip.status not in _DONE
        and clip.check_result is not None
    ]


def _rules(settings: Settings, source_id: int) -> CampaignRules:
    source, _, _ = load_source(settings, source_id)
    if source.campaign_id is None:
        return CampaignRules(min_seconds=20, max_seconds=60)
    with connect(settings.db_path) as db:
        campaign = fetch(db, Campaign, source.campaign_id)
    if campaign is None:
        raise ValueError(f"Campaign {source.campaign_id} does not exist.")
    return campaign.rules


def _inputs(settings: Settings, source_id: int) -> ClipInputs:
    _, _, directory = load_source(settings, source_id)
    rules = _rules(settings, source_id)
    try:
        return source_inputs(
            settings,
            directory,
            rules_hash=input_hash(rules.compliance_payload()),
        )
    except (OSError, ValueError):
        return ClipInputs()


def _show(settings: Settings, source_id: int, clip: Clip, writer: Writer) -> None:
    source, _, directory = load_source(settings, source_id)
    transcript_path = directory / "transcript.json"
    if transcript_path.is_file():
        transcript = Transcript.model_validate_json(transcript_path.read_bytes())
        words = " ".join(
            word.text for word in clip_words(transcript, clip.start_s, clip.end_s)
        )
        spoken = words or "No words fall inside this clip."
    else:
        spoken = "Transcript file is missing."
    reasons = clip.decision_reason if clip.check_result != CheckResult.PASS else ""
    writer(f"Clip {clip.id}")
    writer(f"Path: {clip.final_path or 'no rendered file'}")
    writer(f"Preview: http://127.0.0.1:8765/media/{clip.id}")
    writer(f"Basis: {source.allowed_basis or 'none recorded'}")
    writer(f"Account: {clip.account_id if clip.account_id is not None else 'none'}")
    writer(f"Check: {clip.check_result.value if clip.check_result else 'missing'}")
    if reasons and clip.check_result != CheckResult.PASS:
        writer(f"Reasons: {reasons}")
    writer(f"Transcript: {spoken}")
    writer(f"1 ({clip.hook_a_style or 'unset'}): {clip.hook_a or ''}")
    writer(f"2 ({clip.hook_b_style or 'unset'}): {clip.hook_b or ''}")
    writer(f"Chosen: {clip.chosen_hook or 'none'}")
    writer(f"Caption: {clip.caption or ''}")
    writer(
        "Keys: a approve, r reject, 1 or 2 hook, c caption, t trim, u account, n skip, q quit"
    )


def _mark_changed(clip: Clip) -> None:
    if clip.status in (ClipStatus.APPROVED, ClipStatus.EXPORTED):
        clip.status = ClipStatus.DRAFTED
        clip.changed_since_review = True


def _save(settings: Settings, clip: Clip) -> Clip:
    with connect(settings.db_path) as db:
        save_clip(db, clip)
        stored = fetch(db, Clip, clip.id or 0)
    if stored is None:
        raise ValueError(f"Clip {clip.id} does not exist.")
    return stored


def _recheck(settings: Settings, source_id: int, clip: Clip) -> Clip:
    check_source(settings, source_id, clip_id=clip.id, account_id=clip.account_id)
    with connect(settings.db_path) as db:
        stored = fetch(db, Clip, clip.id or 0)
    if stored is None:
        raise ValueError(f"Clip {clip.id} does not exist.")
    return stored


def _approve(
    settings: Settings, source_id: int, clip: Clip, reader: Reader, writer: Writer
) -> bool:
    if clip.check_result == CheckResult.FAIL:
        writer("A failed check cannot be approved.")
        return False
    if clip.check_result == CheckResult.WARN:
        if _line(reader, "Type y to approve this warning").casefold() != "y":
            writer("Warning was not confirmed.")
            return False
        clip.decision_reason = "confirmed warning"
    elif clip.check_result != CheckResult.PASS:
        writer("Check this clip before approving it.")
        return False
    clip.status = ClipStatus.APPROVED
    clip.reviewed_at = utc_now()
    clip.reviewed_input_hash = stage_input_hash(
        clip, ClipStage.REVIEW, _inputs(settings, source_id)
    )
    clip.changed_since_review = False
    _save(settings, clip)
    writer(f"Clip {clip.id} approved.")
    return True


def _reject(
    settings: Settings, source_id: int, clip: Clip, reader: Reader, writer: Writer
) -> bool:
    reason = _line(reader, "One-word reject reason")
    if len(reason.split()) != 1:
        writer("Reject reason must be one word.")
        return False
    clip.status = ClipStatus.REJECTED
    clip.decision_reason = reason
    clip.reviewed_at = utc_now()
    clip.reviewed_input_hash = stage_input_hash(
        clip, ClipStage.REVIEW, _inputs(settings, source_id)
    )
    clip.changed_since_review = False
    _save(settings, clip)
    writer(f"Clip {clip.id} rejected.")
    return True


def _pick_hook(settings: Settings, source_id: int, clip: Clip, which: str) -> Clip:
    text = clip.hook_a if which == "a" else clip.hook_b
    style = clip.hook_a_style if which == "a" else clip.hook_b_style
    if not text:
        raise ValueError(f"Clip {clip.id} has no hook {which}.")
    clip.chosen_hook = which
    clip.hook_text = text
    clip.hook_style = style
    _mark_changed(clip)
    return _recheck(settings, source_id, _save(settings, clip))


def _edit_caption(settings: Settings, source_id: int, clip: Clip, caption: str) -> Clip:
    if not caption:
        raise ValueError("Caption must not be blank.")
    clip.caption = caption
    _mark_changed(clip)
    return _recheck(settings, source_id, _save(settings, clip))


def _trim(
    settings: Settings,
    source_id: int,
    clip: Clip,
    start_delta: float,
    end_delta: float,
) -> Clip:
    start = clip.start_s + start_delta
    end = clip.end_s + end_delta
    if start < 0 or end <= start:
        raise ValueError("Trim must keep a positive range inside the source.")
    clip.start_s = start
    clip.end_s = end
    _mark_changed(clip)
    stored = _save(settings, clip)
    cut_source(settings, source_id, stored.id)
    caption_source(settings, source_id, stored.id)
    return _recheck(settings, source_id, stored)


def _assign_account(
    settings: Settings, source_id: int, clip: Clip, account_id: int
) -> Clip:
    with connect(settings.db_path) as db:
        account = fetch(db, Account, account_id)
    if account is None:
        raise ValueError(f"Account {account_id} does not exist.")
    clip.account_id = account_id
    return _recheck(settings, source_id, _save(settings, clip))


def review_source(
    settings: Settings,
    source_id: int,
    *,
    clip_id: int | None = None,
    reader: Reader = input,
    writer: Writer = print,
) -> int:
    """Review checked clips. Returns how many were approved or rejected."""
    load_source(settings, source_id)
    skipped: set[int] = set()
    decisions = 0
    while True:
        waiting = _waiting(settings, source_id, skipped)
        if clip_id is not None:
            waiting = [clip for clip in waiting if clip.id == clip_id]
        if not waiting:
            writer("No clips are waiting for review.")
            return decisions
        clip = waiting[0]
        _show(settings, source_id, clip, writer)
        command = _line(reader, "Review key").casefold()
        if command == "q":
            return decisions
        if command == "n":
            if clip.id is not None:
                skipped.add(clip.id)
            continue
        if command == "a":
            decisions += _approve(settings, source_id, clip, reader, writer)
            continue
        if command == "r":
            decisions += _reject(settings, source_id, clip, reader, writer)
            continue
        try:
            if command in {"1", "2"}:
                _pick_hook(settings, source_id, clip, "a" if command == "1" else "b")
            elif command == "c":
                _edit_caption(settings, source_id, clip, _line(reader, "Caption"))
            elif command == "t":
                raw = _line(reader, "Start and end change in seconds")
                start_text, end_text = raw.split()
                _trim(settings, source_id, clip, float(start_text), float(end_text))
            elif command == "u":
                _assign_account(
                    settings, source_id, clip, int(_line(reader, "Account id"))
                )
            else:
                writer(f"Unknown key: {command or 'blank'}")
        except (MediaError, OSError, sqlite3.Error, ValueError) as exc:
            writer(f"Review edit failed: {exc}")


def review(
    source_id: Annotated[int, typer.Argument(min=1)],
    clip: Annotated[int | None, typer.Option(min=1)] = None,
) -> None:
    """Approve or reject the next checked clip from the terminal."""
    try:
        count = review_source(Settings.from_env(), source_id, clip_id=clip)
    except (MediaError, OSError, sqlite3.Error, ValueError) as exc:
        typer.echo(f"Review failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"Decisions recorded: {count}.")
