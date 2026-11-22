"""Database backups and deletion of media that finished clips no longer need."""

import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from montaj.config import Settings
from montaj.db import connect
from montaj.models import Clip, ClipStatus, utc_now

# Posted and rejected clips are done. Every other status can still be rerun.
_FINISHED = frozenset({ClipStatus.POSTED, ClipStatus.REJECTED})


class HousekeepingError(ValueError):
    """Backup or cleanup cannot proceed."""


@dataclass(frozen=True, slots=True)
class Removal:
    path: Path
    size: int


def disk_free(path: Path) -> int:
    """Free bytes on the volume that holds path, or its parent when path is new."""
    target = path if path.exists() else path.parent
    return shutil.disk_usage(target).free


def require_space(
    settings: Settings,
    source_path: Path | None,
    measure: Callable[[Path], int],
) -> None:
    """Refuse before a copy or download that the volume cannot hold.

    A local file must be smaller than the free space. A URL has no size yet, so
    it must clear the configured free-space floor instead.
    """
    try:
        available = measure(settings.data_dir)
    except OSError as exc:
        raise HousekeepingError(
            "Could not read free space on the data volume."
        ) from exc
    free_mb = available / (1024 * 1024)
    if source_path is None:
        if free_mb + 1e-8 >= settings.min_free_mb:
            return
        raise HousekeepingError(
            "Not enough free space to start a download: "
            f"{free_mb:.0f} MB free, need {settings.min_free_mb:.0f} MB."
        )
    try:
        size = source_path.stat().st_size
    except OSError as exc:
        raise HousekeepingError(f"{source_path.name} could not be read.") from exc
    if available >= size:
        return
    raise HousekeepingError(
        f"{source_path.name} is {size / (1024 * 1024):.1f} MB and {free_mb:.1f} MB is free."
    )


def backup_database(settings: Settings, *, now: datetime | None = None) -> Path:
    """Write an online SQLite backup and keep the newest copies."""
    if not settings.db_path.is_file():
        raise HousekeepingError("Database is missing. Run montaj db init first.")
    destination_dir = settings.data_dir / "backups"
    destination_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now or utc_now()).strftime("%Y%m%dT%H%M%S%fZ")
    destination = destination_dir / f"montaj-{stamp}.db"
    source = sqlite3.connect(settings.db_path)
    try:
        target = sqlite3.connect(destination)
        try:
            source.backup(target)
        finally:
            target.close()
    except sqlite3.Error:
        destination.unlink(missing_ok=True)
        raise
    finally:
        source.close()
    _retain(destination_dir, settings.backup_keep)
    return destination


def _retain(directory: Path, keep: int) -> None:
    copies = sorted(path for path in directory.glob("montaj-*.db") if path.is_file())
    for stale in copies[:-keep]:
        stale.unlink()


def plan_cleanup(
    settings: Settings, *, older_than_days: int, now: datetime | None = None
) -> list[Removal]:
    """List old source videos and raw renders that finished clips no longer need."""
    if not settings.db_path.is_file():
        raise HousekeepingError("Database is missing. Run montaj db init first.")
    cutoff = (now or utc_now()).timestamp() - older_than_days * 86400
    clips = _clips(settings.db_path)
    by_source: dict[int, list[Clip]] = {}
    for clip in clips:
        by_source.setdefault(clip.source_id, []).append(clip)
    removals: list[Removal] = []
    for source_id in _source_ids(settings.db_path):
        owned = by_source.get(source_id, [])
        if not owned or any(clip.status not in _FINISHED for clip in owned):
            continue
        _consider(
            removals,
            settings.data_dir / "sources" / str(source_id) / "video.mp4",
            cutoff,
        )
    for clip in clips:
        if clip.id is None or clip.status not in _FINISHED:
            continue
        _consider(
            removals,
            settings.data_dir / "clips" / str(clip.id) / "raw.mp4",
            cutoff,
        )
    return removals


def clean_data(
    settings: Settings,
    *,
    older_than_days: int | None = None,
    dry_run: bool = False,
    now: datetime | None = None,
) -> str:
    """Delete the cleanup plan, or describe it when dry_run is set."""
    days = (
        settings.clean_older_than_days if older_than_days is None else older_than_days
    )
    chosen = plan_cleanup(settings, older_than_days=days, now=now)
    if not dry_run:
        for item in chosen:
            try:
                item.path.unlink()
            except OSError as exc:
                raise HousekeepingError(f"Could not remove {item.path}: {exc}") from exc
    return _describe(chosen, dry_run=dry_run)


def _clips(db_path: Path) -> list[Clip]:
    try:
        with connect(db_path) as db:
            rows = db.execute("SELECT * FROM clips").fetchall()
        return [Clip.model_validate(dict(row)) for row in rows]
    except (sqlite3.Error, ValidationError) as exc:
        raise HousekeepingError(f"Could not read clips: {exc}") from exc


def _source_ids(db_path: Path) -> list[int]:
    try:
        with connect(db_path) as db:
            rows = db.execute("SELECT id FROM sources ORDER BY id").fetchall()
    except sqlite3.Error as exc:
        raise HousekeepingError(f"Could not read sources: {exc}") from exc
    return [int(row["id"]) for row in rows]


def _consider(removals: list[Removal], path: Path, cutoff: float) -> None:
    if not path.is_file():
        return
    stat = path.stat()
    if stat.st_mtime >= cutoff:
        return
    removals.append(Removal(path, stat.st_size))


def _describe(removals: list[Removal], *, dry_run: bool) -> str:
    if not removals:
        return "Nothing to remove."
    verb = "Would remove" if dry_run else "Removed"
    lines = [f"{verb} {item.path} ({item.size} bytes)" for item in removals]
    total = sum(item.size for item in removals)
    action = "Would free" if dry_run else "Freed"
    lines.append(f"{action} {total} bytes.")
    return "\n".join(lines)
