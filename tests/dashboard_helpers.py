"""Shared helpers for the dashboard tests."""

from collections.abc import Callable
from email.message import Message
from io import BytesIO
from pathlib import Path
from threading import Thread
from unittest.mock import Mock
from urllib.parse import urlencode

import pytest

from montaj.config import Settings
from montaj.dashboard import dashboard_response
from montaj.dashboard_jobs import (
    JobQueue,
)
from montaj.dashboard_parts import LiveJob
from montaj.db import connect, insert
from montaj.ingest import SourceMetadata
from montaj.models import Clip, ClipStatus, Niche, Source
from montaj.preview import _handler
from montaj.rank import RankedClip, RankSet

STAGE_PATH = "/dashboard/stage"


def _add_source(
    db_path: Path, *, allowed: bool = True, basis: str = "Creator approval"
) -> None:
    with connect(db_path) as db:
        insert(
            db,
            Source(
                id=7,
                path="input.mp4",
                title="Interview",
                niche=Niche.HEALTH,
                source_allowed=allowed,
                allowed_basis=basis,
            ),
        )


def _home(
    db_path: Path,
    *,
    view: str = "",
    operations: bool = True,
    running: tuple[int, str] | None = None,
    run_spend: tuple[float, float] | None = None,
    live: LiveJob | None = None,
) -> str:
    _, _, body = dashboard_response(
        Settings(data_dir=db_path.parent),
        "/dashboard",
        view,
        operations=operations,
        running=running,
        token="board-token",
        run_spend=run_spend,
        live=live,
    )
    return body.decode()


def _capture_threads(monkeypatch: pytest.MonkeyPatch) -> list[Thread]:
    threads: list[Thread] = []

    def worker(
        *, target: Callable[..., None], args: tuple[object, ...], daemon: bool
    ) -> Thread:
        thread = Thread(target=target, args=args, daemon=daemon)
        threads.append(thread)
        return thread

    monkeypatch.setattr("montaj.dashboard_jobs.Thread", worker)
    return threads


def _post_request(
    settings: Settings,
    jobs: JobQueue | None,
    fields: dict[str, object],
    *,
    origin: str = "http://127.0.0.1:8765",
    token: str | None = None,
    path: str = STAGE_PATH,
) -> Mock:
    handler = _handler(settings, jobs)
    request = object.__new__(handler)
    request.headers = Message()
    request.headers["Host"] = "127.0.0.1:8765"
    request.headers["Origin"] = origin
    request.headers["Content-Type"] = "application/x-www-form-urlencoded"
    valid = jobs.token if jobs is not None else ""
    supplied = token if token is not None else valid
    body = urlencode({"token": supplied, **fields}).encode()
    request.headers["Content-Length"] = str(len(body))
    request.path = path
    request.rfile = BytesIO(body)
    request.wfile = BytesIO()
    request.connection = Mock()
    request.send_error = Mock()
    request.send_response = Mock()
    request.send_header = Mock()
    request.end_headers = Mock()
    request.do_POST()
    return request  # type: ignore[return-value]


def _rows(page: str) -> str:
    """The source cards of the board home."""
    return page.split('<ol class="cards"', 1)[1].split("</ol>", 1)[0]


def _save_rank(db_path: Path, selected: list[int], source: int = 7) -> None:
    clips = [
        RankedClip(clip_id=clip_id, score=8, reason="r", selected=True)
        for clip_id in selected
    ]
    (db_path.parent / f"sources/{source}/rank.json").write_text(
        RankSet(input_hash="h", clips=clips).model_dump_json(), encoding="utf-8"
    )


def _add_clip(
    db_path: Path, status: ClipStatus, source: int = 7, clip_id: int = 1
) -> None:
    """Add a clip and, when a ranking is saved, select it in that ranking."""
    with connect(db_path) as db:
        insert(
            db, Clip(id=clip_id, source_id=source, start_s=0, end_s=5, status=status)
        )
    path = db_path.parent / f"sources/{source}/rank.json"
    if path.is_file():
        selected = RankSet.model_validate_json(path.read_bytes()).selected_ids
        _save_rank(db_path, [*selected, clip_id], source)


def _imported(db_path: Path, *extra: str) -> None:
    folder = db_path.parent / "sources/7"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "meta.json").write_text(
        SourceMetadata(
            title="Interview", duration=72, video_hash="video", audio_hash="audio"
        ).model_dump_json(),
        encoding="utf-8",
    )
    for name in ("video.mp4", "audio.wav", *extra):
        (folder / name).write_bytes(b"fixture")
    if "rank.json" in extra:
        with connect(db_path, read_only=True) as db:
            rows = db.execute("SELECT id FROM clips WHERE source_id = 7 ORDER BY id")
            _save_rank(db_path, [int(row[0]) for row in rows])
