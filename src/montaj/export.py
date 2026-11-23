"""Package approved clips into a folder the operator can upload by hand."""

import csv
import re
import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated

import typer

from montaj.captions import source_inputs
from montaj.config import Settings
from montaj.db import connect, fetch, save_clip
from montaj.ingest import source_dir
from montaj.models import (
    Account,
    Campaign,
    CampaignRules,
    CheckResult,
    Clip,
    ClipStage,
    ClipStatus,
    Source,
    utc_now,
)
from montaj.pipeline import ClipInputs, input_hash, is_stale, stage_input_hash
from montaj.preview import PreviewError, resolve_inside

Writer = Callable[[str], None]
_UNSAFE = re.compile(r"[^A-Za-z0-9]+")


@dataclass(frozen=True, slots=True)
class _Package:
    inputs: ClipInputs
    campaign: str
    hook: str
    caption: str
    video: Path


def _token(value: str, fallback: str) -> str:
    token = _UNSAFE.sub("-", value).strip("-").casefold()
    return token[:48] or fallback


def _paste(hook: str, caption: str, hashtags: list[str]) -> str:
    tags = " ".join(tag for tag in (" ".join(item.split()) for item in hashtags) if tag)
    lines = [hook, "", caption]
    if tags:
        lines.extend(["", tags])
    return "\n".join(lines) + "\n"


def _rules_and_name(
    db: sqlite3.Connection, source: Source
) -> tuple[CampaignRules, str]:
    if source.campaign_id is None:
        return CampaignRules(min_seconds=20, max_seconds=60), ""
    campaign = fetch(db, Campaign, source.campaign_id)
    if campaign is None:
        raise ValueError(f"Campaign {source.campaign_id} does not exist.")
    return campaign.rules, campaign.name


def export_inputs(settings: Settings, source: Source) -> ClipInputs:
    """Build the check and review fingerprints export uses to detect a stale clip."""
    if source.id is None:
        raise ValueError("Source has no id.")
    with connect(settings.db_path) as db:
        rules, _name = _rules_and_name(db, source)
    return source_inputs(
        settings,
        source_dir(settings, source.id),
        rules_hash=input_hash(rules.compliance_payload()),
    )


def _render(settings: Settings, clip: Clip) -> Path:
    if not clip.final_path:
        raise PreviewError("the rendered file is missing.")
    path = resolve_inside(settings.data_dir, clip.final_path)
    if not path.is_file():
        raise PreviewError("the rendered file is missing.")
    return path


def _compliance_reason(clip: Clip, inputs: ClipInputs) -> str | None:
    if clip.check_result is None:
        return "it has not been checked."
    if clip.check_result == CheckResult.FAIL:
        return "the compliance check failed."
    confirmed = clip.decision_reason == "confirmed warning"
    if clip.check_result == CheckResult.WARN and not confirmed:
        return "the warning was not confirmed."
    if clip.check_result != CheckResult.PASS and not confirmed:
        return "it has not been checked."
    if is_stale(clip, ClipStage.CHECK, inputs):
        return "the compliance check is not current."
    if (
        clip.reviewed_at is None
        or clip.changed_since_review
        or is_stale(clip, ClipStage.REVIEW, inputs)
    ):
        return "approval is not current."
    return None


def _package_for(
    settings: Settings, clip: Clip, source: Source | None
) -> _Package | str:
    if source is None or source.id is None:
        return "the source is missing."
    if not source.source_allowed:
        return "the source is not allowed."
    if not (source.allowed_basis or "").strip():
        return "no permission basis is recorded."
    try:
        inputs = export_inputs(settings, source)
        with connect(settings.db_path) as db:
            _rules, campaign = _rules_and_name(db, source)
    except (OSError, ValueError):
        return "inputs are not available."
    reason = _compliance_reason(clip, inputs)
    if reason is not None:
        return reason
    hook = " ".join((clip.hook_text or "").split())
    caption = " ".join((clip.caption or "").split())
    if not hook or not caption:
        return "the chosen hook or caption is missing."
    try:
        video = _render(settings, clip)
    except PreviewError as exc:
        if "outside" in str(exc):
            return "the rendered file is outside the data directory."
        return "the rendered file is missing."
    return _Package(inputs, campaign, hook, caption, video)


def _listed(path: Path, clip_id: int) -> bool:
    if not path.is_file():
        return False
    with path.open(encoding="utf-8", newline="") as handle:
        return any(row and row[0] == str(clip_id) for row in csv.reader(handle))


def _append_manifest(path: Path, row: list[str]) -> None:
    if _listed(path, int(row[0])):
        return
    new_file = not path.is_file()
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        if new_file:
            writer.writerow(
                ["clip_id", "file", "platform", "campaign", "suggested_post_time"]
            )
        writer.writerow(row)


def _write_package(
    folder: Path,
    clip: Clip,
    package: _Package,
    platform: str,
    when: str,
) -> None:
    if clip.id is None:
        raise ValueError("Clip has no id.")
    slug = _token(package.hook, "clip")
    stem = f"{clip.id:02d}_{slug}"
    filename = f"{stem}.mp4"
    folder.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(package.video, folder / filename)
    (folder / f"{stem}.txt").write_text(
        _paste(package.hook, package.caption, clip.hashtags), encoding="utf-8"
    )
    _append_manifest(
        folder / "manifest.csv",
        [str(clip.id), filename, platform, package.campaign, when],
    )


def _mark_exported(
    settings: Settings, clip: Clip, account_id: int, inputs: ClipInputs
) -> None:
    exported = clip.model_copy(
        update={
            "status": ClipStatus.EXPORTED,
            "account_id": account_id if clip.account_id is None else clip.account_id,
            "input_hashes": {
                **clip.input_hashes,
                ClipStage.EXPORT: stage_input_hash(clip, ClipStage.EXPORT, inputs),
            },
        }
    )
    with connect(settings.db_path) as db:
        save_clip(db, exported)


def export_account(
    settings: Settings,
    account_id: int,
    *,
    today: date | None = None,
    now: datetime | None = None,
    writer: Writer = print,
) -> tuple[int, int, Path | None]:
    """Copy approved clips for one account. Returns exported, refused, and the folder."""
    if not settings.db_path.is_file():
        raise ValueError("Database is missing. Run montaj db init first.")
    with connect(settings.db_path) as db:
        account = fetch(db, Account, account_id)
        rows = db.execute(
            "SELECT id FROM clips WHERE status = ? ORDER BY id",
            (ClipStatus.APPROVED.value,),
        ).fetchall()
    if account is None or account.id is None:
        raise ValueError(f"Account {account_id} does not exist.")
    moment = now if now is not None else utc_now()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    moment = moment.astimezone(UTC)
    stamp = (today or moment.date()).isoformat()
    folder = (
        settings.data_dir
        / "exports"
        / stamp
        / f"{account.id}-{_token(account.handle, 'account')}"
    )
    exported = 0
    refused = 0
    written: Path | None = None
    for (clip_id,) in rows:
        with connect(settings.db_path) as db:
            clip = fetch(db, Clip, int(clip_id))
            source = fetch(db, Source, clip.source_id) if clip is not None else None
        if clip is None or clip.id is None or clip.account_id not in (None, account.id):
            continue
        package = _package_for(settings, clip, source)
        if isinstance(package, str):
            writer(f"Clip {clip.id}: {package}")
            refused += 1
            continue
        _write_package(
            folder, clip, package, account.platform.value, moment.isoformat()
        )
        _mark_exported(settings, clip, account.id, package.inputs)
        exported += 1
        written = folder
    if exported == 0 and refused == 0:
        writer("No approved clips are waiting for this account.")
    return exported, refused, written


def export_clips(
    account: Annotated[int, typer.Option("--account", min=1)],
) -> None:
    """Copy approved clips into a dated folder for manual upload."""
    try:
        exported, refused, folder = export_account(
            Settings.from_env(), account, writer=typer.echo
        )
    except (OSError, sqlite3.Error, ValueError) as exc:
        typer.echo(f"Export failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    if exported and folder is not None:
        noun = "clip" if exported == 1 else "clips"
        typer.echo(f"Exported {exported} {noun} to {folder}.")
    if refused:
        raise typer.Exit(1)
