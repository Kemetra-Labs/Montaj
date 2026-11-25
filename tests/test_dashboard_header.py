import re
import sqlite3
from pathlib import Path

import pytest

from montaj.config import Settings
from montaj.costs import Service, Usage, log_api_call
from montaj.dashboard_jobs import JobQueue
from montaj.db import connect, insert
from montaj.models import ClipStatus, Niche, Source
from montaj.preview import handle_preview
from tests.dashboard_helpers import _add_clip, _add_source, _home


def _strip_html(page: str) -> str:
    return (
        '<dl class="summary'
        + page.split('<dl class="summary', 1)[1].split("</dl>", 1)[0]
    )


def _strip(page: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", _strip_html(page)).split())


def _seed_two(db_path: Path, cost: float = 0.012345) -> None:
    _add_source(db_path)
    _add_clip(db_path, ClipStatus.CHECKED)
    with connect(db_path) as db:
        insert(
            db,
            Source(
                id=8,
                path="two.mp4",
                title="Broken",
                niche=Niche.GAMES,
                source_allowed=True,
                speech_status="failed",
            ),
        )
        log_api_call(db, 7, "transcribe", Service.DEEPGRAM, Usage(seconds=12), cost)


def test_strip_text_idle(db_path: Path) -> None:
    _seed_two(db_path)
    assert _strip(_home(db_path)) == (
        "Est. API spend $0.01 2 sources Run ceiling $0.50 No job running "
        "Needs attention 1 Blocked or flagged "
        "Clips awaiting review 1 Checked, from permitted sources"
    )


def test_strip_text_while_a_job_runs(db_path: Path) -> None:
    _seed_two(db_path)
    text = _strip(_home(db_path, running=(7, "render"), run_spend=(0.5, 5.0)))
    assert "Run spend $0.50 of $5.00 run ceiling" in text
    assert "Run ceiling" not in text


def test_strip_links_to_the_filtered_views(db_path: Path) -> None:
    _seed_two(db_path)
    strip = _strip_html(_home(db_path))
    assert 'href="/dashboard?view=attention"' in strip
    assert 'href="/dashboard?view=review"' in strip


def test_strip_tones_follow_the_counts(db_path: Path) -> None:
    _seed_two(db_path)
    busy = _strip_html(_home(db_path))
    assert 'data-tone="warning"' in busy and 'data-tone="review"' in busy


def test_strip_tones_are_neutral_when_nothing_is_flagged(db_path: Path) -> None:
    quiet = _strip_html(_home(db_path))
    assert 'data-tone="warning"' not in quiet and 'data-tone="review"' not in quiet


def test_strip_empty_database(db_path: Path) -> None:
    assert _strip(_home(db_path)) == (
        "Est. API spend $0.00 0 sources Run ceiling $0.50 No job running "
        "Needs attention 0 Nothing flagged "
        "Clips awaiting review 0 None checked yet"
    )


def test_strip_does_not_count_an_unpermitted_source_as_awaiting_review(
    db_path: Path,
) -> None:
    _add_source(db_path)
    with connect(db_path) as db:
        insert(
            db,
            Source(
                id=8,
                path="other.mp4",
                title="Other",
                niche=Niche.HEALTH,
                source_allowed=False,
                allowed_basis=None,
            ),
        )
    _add_clip(db_path, ClipStatus.CHECKED, source=8, clip_id=2)
    assert "Clips awaiting review 0" in _strip(_home(db_path))


def test_strip_escapes_a_tiny_cost(db_path: Path) -> None:
    _seed_two(db_path, cost=0.0002)
    strip = _strip_html(_home(db_path))
    assert "&lt;$0.001" in strip and "<$0.001" not in strip


def test_strip_singular_source(db_path: Path) -> None:
    _add_source(db_path)
    assert "1 source " in _strip(_home(db_path)) + " "


def test_home_header(db_path: Path) -> None:
    _add_source(db_path)
    page = _home(db_path)
    assert page.count("<h1") == 1 and "<h1>Pipeline Control</h1>" in page
    assert "Montaj" in page and "<title>Pipeline Control · Montaj</title>" in page
    assert 'class="rail"' not in page
    assert "Production board" not in page
    header = page.split('<header class="page-head">', 1)[1].split("</header>", 1)[0]
    assert "Refresh" not in header


def test_live_indicator_only_while_a_job_runs(db_path: Path) -> None:
    _add_source(db_path)
    assert 'class="live"' not in _home(db_path)
    assert 'class="live"' in _home(db_path, running=(7, "render"))


def test_read_only_home_has_no_new_job_panel_or_live_indicator(db_path: Path) -> None:
    _add_source(db_path)
    page = _home(db_path, operations=False)
    assert 'id="new-job"' not in page and 'class="live"' not in page
    assert 'class="summary' in page


def _pages(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Every kind of dashboard page, by name."""
    _add_source(db_path)
    settings = Settings(data_dir=db_path.parent)
    jobs = JobQueue(settings)

    def estimate(*args: object) -> list[str]:
        return ["rank: $0.120000"]

    def get(path: str) -> str:
        return handle_preview(settings, path, jobs)[2].decode()

    monkeypatch.setattr("montaj.dashboard_jobs.estimate_run", estimate)
    pages = {
        "home": get("/dashboard"),
        "source": get("/dashboard/sources/7"),
        "confirm": get("/dashboard/sources/7/confirm?stage=rank"),
        "not found": get("/dashboard/sources/99"),
    }

    def broken(*args: object) -> list[str]:
        raise ValueError("raw")

    monkeypatch.setattr("montaj.dashboard_jobs.estimate_run", broken)
    pages["unavailable"] = get("/dashboard/sources/7/confirm?stage=rank")

    def no_db(*args: object, **kwargs: object) -> None:
        raise sqlite3.OperationalError("no db")

    monkeypatch.setattr("montaj.board.build_rows", no_db)
    pages["503"] = get("/dashboard")
    return pages


def test_every_page_has_one_h1_and_no_rail(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, page in _pages(db_path, monkeypatch).items():
        assert page.count("<h1") == 1, name
        assert '<header class="page-head">' in page, name
        assert 'class="rail"' not in page and "Production board" not in page, name


def test_other_pages_link_home_in_the_header(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pages = _pages(db_path, monkeypatch)
    for name in ("source", "confirm", "unavailable", "not found", "503"):
        head = pages[name].split('<header class="page-head">', 1)[1]
        head = head.split("</header>", 1)[0]
        assert '<a class="page-name" href="/dashboard">Pipeline Control</a>' in head
        assert "<h1" not in head, name
    assert "<h1>Pipeline Control</h1>" in pages["home"]
