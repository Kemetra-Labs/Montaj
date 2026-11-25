from datetime import UTC, datetime, timedelta

import pytest

from montaj.board_view import LockInfo
from montaj.dashboard_parts import BoardContext, LiveJob
from montaj.dashboard_states import (
    empty_board_html,
    failed_job_html,
    lock_age_text,
    lock_text,
    retry_html,
)

_NOW = datetime(2026, 11, 25, 12, tzinfo=UTC)
_RETRY = '<a class="button primary" href="/dashboard/sources/7/confirm?stage=batch">Retry</a>'


def _ctx(
    running: tuple[int, str] | None = None, operations: bool = True
) -> BoardContext:
    return BoardContext(
        operations, running, "board-token", source_ceiling=2.0, run_ceiling=0.5
    )


def _failed(source_id: int | None = 7, problem: str = "<raw path C:\\x>") -> LiveJob:
    return LiveJob(
        source_id, "rank", False, 30.0, 0.12, "Stopped during rank.", problem
    )


@pytest.mark.parametrize(
    ("age", "text"),
    [
        (timedelta(seconds=20), "This lock is less than a minute old."),
        (timedelta(seconds=60), "This lock is 1 minute old."),
        (timedelta(minutes=4), "This lock is 4 minutes old."),
        (timedelta(minutes=61), "This lock is 1 hour old."),
        (timedelta(hours=3), "This lock is 3 hours old."),
        (timedelta(minutes=-5), "The lock age is unknown."),
    ],
)
def test_lock_age_text(age: timedelta, text: str) -> None:
    assert lock_age_text(_NOW - age, _NOW) == text


def test_lock_age_text_unknown() -> None:
    assert lock_age_text(None, _NOW) == "The lock age is unknown."


def test_lock_text() -> None:
    started = _NOW - timedelta(minutes=4)
    assert lock_text(LockInfo("pipeline", started), _NOW) == (
        "Locked by pipeline. This lock is 4 minutes old."
    )
    assert lock_text(LockInfo("", None), _NOW) == "Locked. The lock age is unknown."


def test_failed_job_panel() -> None:
    html = failed_job_html(_failed(), {7: "Interview"}, _RETRY)
    assert "rank" in html and "Interview" in html
    assert "The job stopped. The details are in montaj.log" in html
    assert "raw path" not in html and "C:\\x" not in html
    assert "Saved work is kept. Finished stages are reused when you retry." in html
    assert "Run spend $0.12" in html
    assert 'href="/dashboard/sources/7/confirm?stage=batch"' in html
    assert "Latest job" not in html


def test_failed_job_panel_escapes_the_title_and_stage() -> None:
    live = LiveJob(7, "<i>rank</i>", False, 1.0, None, "", "x")
    html = failed_job_html(live, {7: "<b>Talk</b>"}, "")
    assert "<b>" not in html and "<i>" not in html
    assert "&lt;b&gt;Talk&lt;/b&gt;" in html and "Run spend unavailable" in html


def test_failed_job_keeps_a_montaj_sentence() -> None:
    problem = "No more eligible candidates in the ranked batch."
    assert problem in failed_job_html(_failed(problem=problem), {7: "A"}, _RETRY)


def test_retry_html() -> None:
    titles = {7: "Interview"}
    assert retry_html(_failed(), titles, None, _ctx()) == _RETRY
    assert "/confirm" not in retry_html(_failed(), titles, None, _ctx((9, "batch")))
    assert retry_html(_failed(), titles, None, _ctx(operations=False)) == ""


def test_retry_html_withheld() -> None:
    from tests.test_board_view import _card_for, _states

    titles = {1: "A"}
    live = _failed(source_id=1)
    locked = _card_for(_states(), locks={1: LockInfo("rank", None)})
    assert "Locked by another command" in retry_html(live, titles, locked, _ctx())
    over = _card_for(_states(cost=2.0))
    assert "Over the source ceiling" in retry_html(live, titles, over, _ctx())
    blocked = _card_for(_states(allowed=False))
    assert retry_html(live, titles, blocked, _ctx()) == ""
    for html in (
        retry_html(live, titles, locked, _ctx()),
        retry_html(live, titles, over, _ctx()),
    ):
        assert "/confirm" not in html and "<button disabled" in html


def test_failed_import_panel() -> None:
    live = _failed(source_id=None)
    html = failed_job_html(live, {}, retry_html(live, {}, None, _ctx()))
    assert "Start it again from New job." in html
    assert "/confirm" not in html and "a new import" in html


def test_unknown_source_is_not_retried() -> None:
    assert "/confirm" not in retry_html(_failed(), {}, None, _ctx())


def test_empty_board_html() -> None:
    html = empty_board_html(True)
    assert "No runs yet" in html and "montaj ingest" in html
    assert 'href="#new-job"' in html
    read_only = empty_board_html(False)
    assert "No runs yet" in read_only and "then refresh" in read_only
    assert "href=" not in read_only and "<button" not in read_only


def test_retry_is_withheld_after_a_source_ceiling_stop() -> None:
    problem = (
        "draft: per-source budget ceiling $2.000000; estimated spend $1.950000, "
        "next call $0.100000."
    )
    live = _failed(problem=problem)
    html = retry_html(live, {7: "Interview"}, None, _ctx())
    assert "/confirm" not in html and "Over the source ceiling" in html
    assert "<button disabled" in html


def test_failed_panel_keeps_the_summary_and_review_link() -> None:
    live = LiveJob(
        7, "draft", False, 9.0, 0.5, "Stopped during draft after 7 clips.", "x"
    )
    html = failed_job_html(live, {7: "Interview"}, _RETRY, review=True)
    assert "Stopped during draft after 7 clips." in html
    assert 'href="/?source=7&amp;ready=1">Review clips</a>' in html
    assert "Review clips" not in failed_job_html(live, {7: "Interview"}, _RETRY)


def test_failed_panel_escapes_the_summary() -> None:
    live = LiveJob(7, "draft", False, 9.0, 0.5, "<i>x</i>", "x")
    assert "<i>" not in failed_job_html(live, {7: "A"}, "")


def test_lock_text_says_how_to_clear_a_stale_lock() -> None:
    from montaj.dashboard_states import stale_lock_text

    text = stale_lock_text(7)
    assert "If no Montaj command is running, the lock is stale" in text
    assert "sources/7/.lock-holder" in text
