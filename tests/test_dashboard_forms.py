"""Every form, button and action link on the source and confirm pages, pinned.

Written before MJ-28 moved these pages into the board shell, and its expected
values were taken from the code before that move: the markup around these
controls may change, but what they post, link to and say may not. Inventories
are sorted, so a layout change that only reorders panels still passes.
"""

import re
from pathlib import Path

import pytest

from montaj.board import CellState, StageCell
from montaj.budget import Budget
from montaj.config import Settings
from montaj.dashboard import dashboard_response
from montaj.dashboard_detail import _stage_action_html
from montaj.dashboard_jobs import JobQueue
from montaj.dashboard_parts import BoardContext, LiveJob
from montaj.db import connect
from montaj.models import ClipStatus
from montaj.preview import handle_preview
from tests.dashboard_helpers import _add_clip, _add_source, _imported

_FORM = re.compile(r"<form\b([^>]*)>(.*?)</form>", re.S)
_ATTR = re.compile(r'(\w+)="([^"]*)"')
_HIDDEN = re.compile(r'<input type="hidden" name="([^"]+)" value="([^"]*)"')
_BUTTON = re.compile(r"<button\b([^>]*)>(.*?)</button>", re.S)
_LINK = re.compile(
    r'<a\b[^>]*href="(/\?source=[^"]*|[^"]*/confirm[^"]*)"[^>]*>(.*?)</a>', re.S
)
_TAG = re.compile(r"<[^>]+>")
_TOKEN = "board-token"
_SAVED = ("transcript.json", "candidates.json", "rank.json")

Inventory = list[tuple[str, ...]]


def _text(html: str) -> str:
    return " ".join(_TAG.sub(" ", html).split())


def _inventory(page: str, token: str) -> Inventory:
    found: Inventory = []
    for attrs, body in _FORM.findall(page):
        named = dict(_ATTR.findall(attrs))
        fields = sorted(
            (name, "TOKEN" if value == token else value)
            for name, value in _HIDDEN.findall(body)
        )
        found.append(
            ("form", named.get("action", ""), named.get("method", ""), repr(fields))
        )
    for attrs, body in _BUTTON.findall(page):
        state = "disabled" if "disabled" in attrs.split() else "enabled"
        found.append(("button", state, _text(body)))
    found.extend(("link", href, _text(body)) for href, body in _LINK.findall(page))
    return sorted(found)


def _source_page(
    db_path: Path,
    *,
    operations: bool = True,
    running: tuple[int, str] | None = None,
    live: LiveJob | None = None,
) -> str:
    status, _, body = dashboard_response(
        Settings(data_dir=db_path.parent),
        "/dashboard/sources/7",
        "",
        operations=operations,
        running=running,
        token=_TOKEN,
        live=live,
    )
    assert status == 200
    return body.decode()


def _scenario(db_path: Path, name: str) -> str:
    _add_source(db_path)
    if name == "done and pending":
        _imported(db_path, *_SAVED)
        return _source_page(db_path)
    if name == "failed paid stage":
        _imported(db_path)
        with connect(db_path) as db:
            db.execute("UPDATE sources SET speech_status = 'failed' WHERE id = 7")
        return _source_page(db_path)
    if name == "busy on this source":
        _imported(db_path, *_SAVED)
        return _source_page(db_path, running=(7, "render"))
    if name == "busy on another source":
        _imported(db_path, *_SAVED)
        return _source_page(db_path, running=(2, "render"))
    _imported(db_path, *_SAVED)
    _add_clip(db_path, ClipStatus.CHECKED)
    _add_clip(db_path, ClipStatus.APPROVED, clip_id=2)
    if name == "clips and a finished job":
        live = LiveJob(7, "Done", False, 4.0, 0.1, "Made 2 clips.", "")
        return _source_page(db_path, live=live)
    assert name == "clips, read only"
    return _source_page(db_path, operations=False)


_SCENARIOS = (
    "done and pending",
    "failed paid stage",
    "busy on this source",
    "busy on another source",
    "clips and a finished job",
    "clips, read only",
)


@pytest.mark.parametrize("name", _SCENARIOS)
def test_source_page_controls_are_unchanged(db_path: Path, name: str) -> None:
    assert _inventory(_scenario(db_path, name), _TOKEN) == EXPECTED[name]


def test_free_stage_retry_posts_retry() -> None:
    ctx = BoardContext(True, None, _TOKEN, source_ceiling=2.0, run_ceiling=0.5)
    cell = StageCell("render", "Cut + captions", CellState.FAILED)
    html = _stage_action_html(cell, 7, ctx)
    assert _inventory(html, _TOKEN) == EXPECTED["free stage retry"]


@pytest.fixture
def board(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Settings, JobQueue]:
    def fake_estimate(
        settings: Settings, source: int, until: str, language: str, budget: Budget
    ) -> list[str]:
        return [f"{until}: $0.120000"]

    monkeypatch.setattr("montaj.dashboard_jobs.estimate_run", fake_estimate)
    _add_source(db_path)
    settings = Settings(data_dir=db_path.parent)
    return settings, JobQueue(settings)


@pytest.mark.parametrize("query", ["stage=rank", "stage=rank&retry=yes", "stage=batch"])
def test_confirm_pages(board: tuple[Settings, JobQueue], query: str) -> None:
    settings, jobs = board
    status, _, body = handle_preview(
        settings, f"/dashboard/sources/7/confirm?{query}", jobs
    )
    assert status == 200
    assert _inventory(body.decode(), jobs.token) == EXPECTED[query]


EXPECTED: dict[str, Inventory] = {
    "done and pending": [
        ("button", "enabled", "Run"),
        (
            "form",
            "/dashboard/stage",
            "post",
            "[('source', '7'), ('stage', 'render'), ('token', 'TOKEN')]",
        ),
        ("link", "/dashboard/sources/7/confirm?stage=rank", "Rerun"),
        ("link", "/dashboard/sources/7/confirm?stage=transcribe", "Rerun"),
    ],
    "failed paid stage": [
        (
            "link",
            "/dashboard/sources/7/confirm?stage=transcribe&amp;retry=yes",
            "Retry (confirm cost)",
        ),
    ],
    "busy on this source": [
        ("button", "disabled", "Rerun"),
        ("button", "disabled", "Rerun"),
    ],
    "busy on another source": [
        ("button", "disabled", "Rerun"),
        ("button", "disabled", "Rerun"),
        ("button", "disabled", "Run"),
    ],
    "clips and a finished job": [
        ("button", "enabled", "Rerun"),
        (
            "form",
            "/dashboard/stage",
            "post",
            "[('source', '7'), ('stage', 'render'), ('token', 'TOKEN')]",
        ),
        ("link", "/?source=7", "Open review"),
        ("link", "/?source=7", "Preview clips ↗"),
        ("link", "/?source=7&amp;ready=1", "Review clips"),
        ("link", "/dashboard/sources/7/confirm?stage=draft", "Rerun"),
        ("link", "/dashboard/sources/7/confirm?stage=rank", "Rerun"),
        ("link", "/dashboard/sources/7/confirm?stage=transcribe", "Rerun"),
    ],
    "clips, read only": [
        ("link", "/?source=7", "Open review"),
        ("link", "/?source=7", "Preview clips ↗"),
    ],
    "free stage retry": [
        ("button", "enabled", "Retry"),
        (
            "form",
            "/dashboard/stage",
            "post",
            "[('retry', 'yes'), ('source', '7'), ('stage', 'render'), ('token', 'TOKEN')]",
        ),
    ],
    "stage=rank": [
        ("button", "enabled", "Confirm and run"),
        (
            "form",
            "/dashboard/stage",
            "post",
            "[('confirm', 'yes'), ('source', '7'), ('stage', 'rank'), ('token', 'TOKEN')]",
        ),
    ],
    "stage=rank&retry=yes": [
        ("button", "enabled", "Confirm and run"),
        (
            "form",
            "/dashboard/stage",
            "post",
            "[('confirm', 'yes'), ('retry', 'yes'), ('source', '7'), ('stage', 'rank'), ('token', 'TOKEN')]",
        ),
    ],
    "stage=batch": [
        ("button", "enabled", "Confirm and run"),
        (
            "form",
            "/dashboard/jobs",
            "post",
            "[('confirm', 'yes'), ('resume', '7'), ('token', 'TOKEN'), ('top', '10')]",
        ),
    ],
}
