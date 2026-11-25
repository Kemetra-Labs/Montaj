"""Every form and confirm link on the source and confirm pages, pinned.

Written before MJ-28 moved these pages into the board shell: the markup around
them may change, but what they post and link to may not.
"""

import re
from pathlib import Path

import pytest

from montaj.budget import Budget
from montaj.config import Settings
from montaj.dashboard_jobs import JobQueue
from montaj.db import connect
from montaj.preview import handle_preview
from tests.dashboard_helpers import _add_source, _imported

_FORM = re.compile(r"<form\b([^>]*)>(.*?)</form>", re.S)
_ATTR = re.compile(r'(\w+)="([^"]*)"')
_HIDDEN = re.compile(r'<input type="hidden" name="([^"]+)" value="([^"]*)"')
_CONFIRM_HREF = re.compile(r'href="([^"]*/confirm[^"]*)"')

Inventory = list[tuple[str, ...]]


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
    found.extend(("link", href) for href in _CONFIRM_HREF.findall(page))
    return found


def _get(settings: Settings, jobs: JobQueue, path: str) -> str:
    status, _, body = handle_preview(settings, path, jobs)
    assert status == 200
    return body.decode()


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


def test_source_page_with_done_and_pending_stages(
    db_path: Path, board: tuple[Settings, JobQueue]
) -> None:
    settings, jobs = board
    _imported(db_path, "transcript.json", "candidates.json", "rank.json")
    page = _get(settings, jobs, "/dashboard/sources/7")
    assert _inventory(page, jobs.token) == EXPECTED_DONE_AND_PENDING


def test_source_page_with_a_failed_stage(
    db_path: Path, board: tuple[Settings, JobQueue]
) -> None:
    settings, jobs = board
    _imported(db_path)
    with connect(db_path) as db:
        db.execute("UPDATE sources SET speech_status = 'failed' WHERE id = 7")
    page = _get(settings, jobs, "/dashboard/sources/7")
    assert _inventory(page, jobs.token) == EXPECTED_FAILED


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("stage=rank", "EXPECTED_CONFIRM_RANK"),
        ("stage=rank&retry=yes", "EXPECTED_CONFIRM_RANK_RETRY"),
        ("stage=batch", "EXPECTED_CONFIRM_BATCH"),
    ],
)
def test_confirm_pages(
    board: tuple[Settings, JobQueue], query: str, expected: str
) -> None:
    settings, jobs = board
    page = _get(settings, jobs, f"/dashboard/sources/7/confirm?{query}")
    assert _inventory(page, jobs.token) == globals()[expected]


EXPECTED_DONE_AND_PENDING: Inventory = [
    (
        "form",
        "/dashboard/stage",
        "post",
        "[('source', '7'), ('stage', 'render'), ('token', 'TOKEN')]",
    ),
    ("link", "/dashboard/sources/7/confirm?stage=transcribe"),
    ("link", "/dashboard/sources/7/confirm?stage=rank"),
]
EXPECTED_FAILED: Inventory = [
    ("link", "/dashboard/sources/7/confirm?stage=transcribe&amp;retry=yes"),
]
EXPECTED_CONFIRM_RANK: Inventory = [
    (
        "form",
        "/dashboard/stage",
        "post",
        "[('confirm', 'yes'), ('source', '7'), ('stage', 'rank'), ('token', 'TOKEN')]",
    ),
]
EXPECTED_CONFIRM_RANK_RETRY: Inventory = [
    (
        "form",
        "/dashboard/stage",
        "post",
        "[('confirm', 'yes'), ('retry', 'yes'), ('source', '7'), ('stage', 'rank'), "
        "('token', 'TOKEN')]",
    ),
]
EXPECTED_CONFIRM_BATCH: Inventory = [
    (
        "form",
        "/dashboard/jobs",
        "post",
        "[('confirm', 'yes'), ('resume', '7'), ('token', 'TOKEN'), ('top', '10')]",
    ),
]
