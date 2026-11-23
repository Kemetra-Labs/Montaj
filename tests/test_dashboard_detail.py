import wave
from pathlib import Path

import pytest

from montaj.budget import Budget, BudgetExceeded
from montaj.config import Settings
from montaj.costs import Service, Usage, log_api_call
from montaj.dashboard import LiveJob, dashboard_response
from montaj.dashboard_jobs import (
    JobQueue,
    StageSubmission,
)
from montaj.db import connect, insert
from montaj.media import MediaError
from montaj.models import ClipStatus, Niche, Source
from montaj.preview import handle_preview
from montaj.stage_jobs import BoardStage, StageResult
from tests.dashboard_helpers import (
    _add_clip,
    _add_source,
    _capture_threads,
    _imported,
)


def _detail_page(
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
        token="detail-token",
        live=live,
    )
    assert status == 200
    return body.decode()


def _stage_lines(page: str) -> dict[str, str]:
    """The markup of each stage line, keyed by its label."""
    block = page.split('<ol class="steps stage-lines"', 1)[1].split("</ol>", 1)[0]
    lines = block.split("<li")[1:]
    return {
        line.split('class="cell-name">', 1)[1].split("<", 1)[0]: line for line in lines
    }


def test_detail_lists_every_stage_with_status_and_action(db_path: Path) -> None:
    _add_source(db_path)
    _imported(db_path)
    lines = _stage_lines(_detail_page(db_path))
    assert list(lines) == [
        "Ingest",
        "Transcribe",
        "Candidates + rank",
        "Cut + captions",
        "Draft + check",
        "Review",
        "Export",
    ]
    assert "Done" in lines["Ingest"] and "<a" not in lines["Ingest"]
    assert (
        "Pending" in lines["Transcribe"] and "Run (confirm cost)" in lines["Transcribe"]
    )
    for label in ("Candidates + rank", "Cut + captions", "Draft + check"):
        assert "Blocked" in lines[label] and "Transcribe first" in lines[label]
        assert "<a" not in lines[label] and "<button" not in lines[label]
    for label in ("Review", "Export"):
        assert "<a" not in lines[label] and "<button" not in lines[label]
    with connect(db_path) as db:
        db.execute("UPDATE sources SET speech_status = 'failed' WHERE id = 7")
    failed = _stage_lines(_detail_page(db_path))["Transcribe"]
    assert "Failed" in failed and "Montaj recorded a source failure" in failed
    assert "Retry (confirm cost)" in failed and "retry=yes" in failed
    assert "Run (confirm cost)" not in failed


def test_paid_stage_links_to_confirm_and_free_stage_posts_directly(
    db_path: Path,
) -> None:
    _add_source(db_path)
    _imported(db_path)
    paid = _stage_lines(_detail_page(db_path))["Transcribe"]
    assert 'href="/dashboard/sources/7/confirm?stage=transcribe"' in paid
    assert "<form" not in paid and "<button" not in paid
    _imported(db_path, "transcript.json", "candidates.json", "rank.json")
    free = _stage_lines(_detail_page(db_path))["Cut + captions"]
    assert 'action="/dashboard/stage" method="post"' in free
    assert 'name="token" value="detail-token"' in free
    assert 'name="source" value="7"' in free and 'name="stage" value="render"' in free
    assert "retry" not in free and "confirm" not in free
    assert free.count("<button") == 1 and "/confirm" not in free


def test_detail_read_only_shows_status_but_no_controls(db_path: Path) -> None:
    _add_source(db_path)
    _imported(db_path)
    page = _detail_page(db_path, operations=False)
    assert "Pending" in _stage_lines(page)["Transcribe"]
    assert "<form" not in page and "<button" not in page and "/confirm" not in page


def test_detail_buttons_are_disabled_while_a_job_runs(db_path: Path) -> None:
    _add_source(db_path)
    _imported(db_path, "transcript.json", "candidates.json", "rank.json")
    page = _detail_page(db_path, running=(2, "render"))
    free = _stage_lines(page)["Cut + captions"]
    assert "<button disabled" in free and "<form" not in free
    assert "Another job is running (source 2)" in free
    assert 'aria-describedby="why-render"' in free and 'id="why-render"' in free
    assert "<form" not in page and "/confirm" not in page
    (db_path.parent / "sources/7/transcript.json").unlink()
    paid = _stage_lines(_detail_page(db_path, running=(0, "batch")))["Transcribe"]
    assert "<button disabled" in paid and "/confirm" not in paid
    assert "Another job is running" in paid and "(source" not in paid


def test_detail_links_to_clip_preview(db_path: Path) -> None:
    _add_source(db_path)
    _imported(db_path)
    assert "Preview clips" not in _detail_page(db_path)
    _add_clip(db_path, ClipStatus.CHECKED)
    page = _detail_page(db_path)
    assert 'href="/?source=7">Preview clips' in page
    lines = _stage_lines(page)
    assert 'href="/?source=7"' in lines["Review"] and "Open review" in lines["Review"]
    assert "Open review" not in lines["Export"]


def test_detail_header_shows_campaign_and_source_spend(db_path: Path) -> None:
    with connect(db_path) as db:
        db.execute(
            "INSERT INTO campaigns (id, name, platform, rules_json) VALUES (3, ?, 'tiktok', '{}')",
            ("<b>Spring</b>",),
        )
        insert(
            db,
            Source(
                id=7,
                path="input.mp4",
                title="Interview",
                niche=Niche.HEALTH,
                campaign_id=3,
                source_allowed=True,
                allowed_basis="Creator approval",
            ),
        )
        log_api_call(db, 7, "transcribe", Service.DEEPGRAM, Usage(seconds=1), 0.5)
    page = _detail_page(db_path)
    assert "&lt;b&gt;Spring&lt;/b&gt;" in page and "<b>Spring" not in page
    assert "$0.500000 of $2.00 source ceiling" in page


def test_detail_shows_live_job_panel(db_path: Path) -> None:
    _add_source(db_path)
    running = LiveJob(7, "Transcribing", True, 12.4, 0.25, "", "")
    page = _detail_page(db_path, running=(7, "transcribe"), live=running)
    assert "Transcribing" in page and "Elapsed 12 s" in page and "$0.250000" in page
    assert '<meta http-equiv="refresh" content="3">' in page
    done = LiveJob(
        7,
        "Starting",
        False,
        3.0,
        0.1,
        "Stopped during transcribe.",
        "<i>Another process holds the lock (pid 5)</i>",
    )
    finished = _detail_page(db_path, live=done)
    assert "Stopped during transcribe." in finished
    assert "&lt;i&gt;Another process holds the lock (pid 5)&lt;/i&gt;" in finished
    assert "<i>" not in finished and 'http-equiv="refresh"' not in finished
    other = LiveJob(8, "x", True, 1, 0, "", "")
    assert "Live job" not in _detail_page(db_path, live=other)
    importing = LiveJob(None, "x", True, 1, 0, "", "")
    assert "Live job" not in _detail_page(db_path, live=importing)
    assert "Live job" not in _detail_page(db_path)


def _confirm(
    settings: Settings, jobs: JobQueue | None, source: int, query: str
) -> tuple[int, bytes]:
    status, _, body = handle_preview(
        settings, f"/dashboard/sources/{source}/confirm?{query}", jobs
    )
    return status, body


def test_confirm_page_shows_estimate_lines_and_confirm_form(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_source(db_path)
    settings = Settings(data_dir=db_path.parent)
    jobs = JobQueue(settings)
    seen: list[tuple[int, str, float]] = []

    def fake_estimate(
        settings: Settings, source: int, until: str, language: str, budget: Budget
    ) -> list[str]:
        seen.append((source, until, budget.per_run))
        return [
            "Source 7 prior estimated API spend: <b>$0.10</b>",
            "transcribe: $0.120000 for one full audio pass",
            "rank: remaining cost depends on inputs and cache reuse",
        ]

    monkeypatch.setattr("montaj.dashboard_jobs.estimate_run", fake_estimate)
    status, body = _confirm(settings, jobs, 7, "stage=rank")
    page = body.decode()
    assert status == 200 and seen == [(7, "rank", settings.max_cost_per_run_usd)]
    assert "prior estimated API spend: &lt;b&gt;$0.10&lt;/b&gt;" in page
    assert "<b>" not in page
    # Only the lines for this stage, and the ceilings that stop its spend.
    assert "rank: remaining cost" in page and "transcribe:" not in page
    assert "No offline estimate for candidates and rank; spend stops at" in page
    assert 'action="/dashboard/stage" method="post"' in page
    for field in (
        f'name="token" value="{jobs.token}"',
        'name="source" value="7"',
        'name="stage" value="rank"',
        'name="confirm" value="yes"',
    ):
        assert field in page
    assert 'name="retry"' not in page and page.count("<button") == 1
    assert 'href="/dashboard/sources/7">Cancel' in page
    retry = _confirm(settings, jobs, 7, "stage=rank&retry=yes")[1].decode()
    assert 'name="retry" value="yes"' in retry


@pytest.mark.parametrize(
    "error",
    [
        ValueError("raw /secret/path detail"),
        MediaError("raw ffmpeg detail"),
        OSError("raw os detail"),
        wave.Error("raw wave detail"),
        EOFError("raw eof detail"),
    ],
)
def test_confirm_page_hides_a_failed_estimate(
    db_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    _add_source(db_path)
    settings = Settings(data_dir=db_path.parent)

    def failing(*_: object, **__: object) -> list[str]:
        raise error

    monkeypatch.setattr("montaj.dashboard_jobs.estimate_run", failing)
    status, body = _confirm(settings, JobQueue(settings), 7, "stage=transcribe")
    page = body.decode()
    assert status == 200
    assert "Estimate unavailable. Check the source before running this stage." in page
    assert "raw" not in page and "<form" not in page and "<button" not in page
    assert 'href="/dashboard/sources/7"' in page


@pytest.mark.parametrize(
    ("source", "query"),
    [(7, "stage=bogus"), (7, ""), (7, "stage=render"), (99, "stage=rank")],
)
def test_confirm_unknown_stage_is_404(
    db_path: Path, monkeypatch: pytest.MonkeyPatch, source: int, query: str
) -> None:
    _add_source(db_path)
    settings = Settings(data_dir=db_path.parent)
    monkeypatch.setattr(
        "montaj.dashboard_jobs.estimate_run", lambda *a, **k: ["never shown"]
    )
    status, body = _confirm(settings, JobQueue(settings), source, query)
    assert status == 404 and b"never shown" not in body
    assert b"not found" in body.lower()


def test_confirm_page_is_not_served_read_only(db_path: Path) -> None:
    _add_source(db_path)
    status, _ = _confirm(Settings(data_dir=db_path.parent), None, 7, "stage=rank")
    assert status == 404


def test_detail_for_an_unpermitted_source_has_no_actions_and_says_why(
    db_path: Path,
) -> None:
    _add_source(db_path, allowed=False, basis="")
    _add_clip(db_path, ClipStatus.CHECKED)
    _imported(db_path)
    page = _detail_page(db_path)
    for absent in ("<form", "<button", "/confirm", "Open review", "Preview clips"):
        assert absent not in page
    lines = _stage_lines(page)
    assert len(lines) == 7
    assert all("Blocked" in line for line in lines.values())
    # The reason shows once as the page notice, in one wording, not on each line.
    reason = "Record permission for this source before processing it."
    assert page.count(reason) == 1
    assert "Record permission for this source first." not in page


def test_detail_for_a_source_without_a_basis_is_blocked_and_the_server_refuses(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_source(db_path, allowed=True, basis="  ")
    _imported(db_path)
    _add_clip(db_path, ClipStatus.CHECKED)
    page = _detail_page(db_path)
    for absent in ("<form", "<button", "/confirm", "Open review", "Preview clips"):
        assert absent not in page
    lines = _stage_lines(page)
    assert all("Blocked" in line for line in lines.values())
    assert page.count("Record a permission basis for this source first.") == 1
    settings = Settings(data_dir=db_path.parent)
    jobs = JobQueue(settings)
    monkeypatch.setattr(
        "montaj.dashboard_jobs.estimate_run", lambda *a, **k: ["never shown"]
    )
    status, body = _confirm(settings, jobs, 7, "stage=transcribe")
    assert status == 200 and b"never shown" not in body and b"<form" not in body
    assert b"Estimate unavailable" in body
    with pytest.raises(ValueError, match="recorded permission"):
        jobs.submit_stage(StageSubmission(source=7, stage=BoardStage.RENDER))


def test_up_to_date_stage_offers_explicit_rerun(db_path: Path) -> None:
    _add_source(db_path)
    _imported(db_path, "transcript.json", "candidates.json", "rank.json")
    _add_clip(db_path, ClipStatus.CHECKED)
    lines = _stage_lines(_detail_page(db_path))
    for label in ("Transcribe", "Candidates + rank", "Draft + check"):
        assert "Done" in lines[label] and "Up to date" in lines[label]
        assert ">Rerun</a>" in lines[label] and "<form" not in lines[label]
        assert "(confirm cost)" not in lines[label]
    assert (
        'href="/dashboard/sources/7/confirm?stage=rank"' in lines["Candidates + rank"]
    )
    assert "retry=yes" not in lines["Transcribe"]
    free = lines["Cut + captions"]
    assert "Up to date" in free and ">Rerun</button>" in free
    assert 'action="/dashboard/stage" method="post"' in free
    assert 'name="stage" value="render"' in free and 'name="retry"' not in free
    assert "Up to date" not in lines["Ingest"] and "<a" not in lines["Ingest"]
    for label in ("Review", "Export"):
        assert "Up to date" not in lines[label] and "Rerun" not in lines[label]


def test_rerun_is_disabled_while_a_job_runs_and_absent_read_only(
    db_path: Path,
) -> None:
    _add_source(db_path)
    _imported(db_path, "transcript.json", "candidates.json", "rank.json")
    busy = _stage_lines(_detail_page(db_path, running=(2, "render")))
    assert "<button disabled" in busy["Transcribe"] and "Rerun" in busy["Transcribe"]
    assert "Another job is running (source 2)" in busy["Transcribe"]
    read_only = _stage_lines(_detail_page(db_path, operations=False))["Transcribe"]
    assert "Up to date" in read_only and "Rerun" not in read_only


def test_failed_check_shows_the_reason_on_review_and_export_lines(
    db_path: Path,
) -> None:
    _add_source(db_path)
    _imported(db_path, "transcript.json", "candidates.json", "rank.json")
    _add_clip(db_path, ClipStatus.NEEDS_FIX)
    lines = _stage_lines(_detail_page(db_path))
    for label in ("Review", "Export"):
        assert "Check has failures. Fix the clips first." in lines[label]
        assert "<a" not in lines[label] and "<button" not in lines[label]


def test_over_budget_stop_is_shown_on_detail(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_source(db_path)
    settings = Settings(data_dir=db_path.parent)
    jobs = JobQueue(settings)
    threads = _capture_threads(monkeypatch)
    stop = BudgetExceeded(
        "rank: per-run budget ceiling $0.500000; estimated spend $0.400000, "
        "next call $0.200000. <b>stop</b>"
    )
    monkeypatch.setattr(
        "montaj.dashboard_jobs.run_board_stage",
        lambda *a, **k: StageResult("Stopped during rank.", problem=str(stop)),
    )
    jobs.submit_stage(StageSubmission(source=7, stage=BoardStage.RANK, confirm=True))
    threads[0].join(timeout=5)
    page = handle_preview(settings, "/dashboard/sources/7", jobs)[2].decode()
    assert "Stopped during rank." in page
    assert "per-run budget ceiling $0.500000" in page
    assert "&lt;b&gt;stop&lt;/b&gt;" in page and "<b>stop" not in page
    assert ">Review clips</a>" not in page


def _clips(db_path: Path, statuses: list[ClipStatus]) -> None:
    for clip_id, status in enumerate(statuses, start=1):
        _add_clip(db_path, status, clip_id=clip_id)


def test_partly_rendered_batch_after_a_restart_offers_run_not_rerun(
    db_path: Path,
) -> None:
    _add_source(db_path)
    _imported(db_path, "transcript.json", "candidates.json", "rank.json")
    _clips(db_path, [ClipStatus.CAPTIONED] * 3 + [ClipStatus.RANKED] * 7)
    settings = Settings(data_dir=db_path.parent)
    restarted = JobQueue(settings)
    page = handle_preview(settings, "/dashboard/sources/7", restarted)[2].decode()
    render = _stage_lines(page)["Cut + captions"]
    assert "Pending" in render and "3 of 10 done" in render
    assert "Up to date" not in render and "Rerun" not in render
    assert ">Run</button>" in render
    home = handle_preview(settings, "/dashboard", restarted)[2].decode()
    assert "Cut + captions: 3 of 10 done" in home and "Resume" in home


def test_render_is_not_up_to_date_after_a_new_ranking(db_path: Path) -> None:
    _add_source(db_path)
    _imported(db_path, "transcript.json", "candidates.json", "rank.json")
    _clips(db_path, [ClipStatus.CAPTIONED, ClipStatus.CAPTIONED])
    assert "Up to date" in _stage_lines(_detail_page(db_path))["Cut + captions"]
    _add_clip(db_path, ClipStatus.RANKED, clip_id=3)
    (db_path.parent / "sources/7/rank.json").write_text(
        '{"input_hash":"new","clips":[{"clip_id":3,"score":9,"reason":"r","selected":true}]}',
        encoding="utf-8",
    )
    render = _stage_lines(_detail_page(db_path))["Cut + captions"]
    assert "Up to date" not in render and "Done" not in render


def test_flagged_clips_show_fix_guidance_without_a_rerun(db_path: Path) -> None:
    _add_source(db_path)
    _imported(db_path, "transcript.json", "candidates.json", "rank.json")
    _clips(db_path, [ClipStatus.NEEDS_FIX, ClipStatus.CHECKED])
    guidance = (
        "Fix the flagged clips in the clip preview or with montaj review, "
        "then rerun the check."
    )
    lines = _stage_lines(_detail_page(db_path))
    draft = lines["Draft + check"]
    assert "Failed" in draft and f"1 flagged clip. {guidance}" in draft
    assert "<a" not in draft and "<button" not in draft
    assert "Pending" in lines["Review"] and "Open review" in lines["Review"]
    with connect(db_path) as db:
        db.execute("UPDATE clips SET status = 'needs_fix' WHERE id = 2")
    lines = _stage_lines(_detail_page(db_path))
    assert f"2 flagged clips. {guidance}" in lines["Draft + check"]
    assert "Blocked" in lines["Review"] and "Open review" not in lines["Review"]


@pytest.mark.parametrize("status", ["language_mismatch", "no_usable_speech"])
def test_speech_language_failure_has_no_retry_on_detail(
    db_path: Path, status: str
) -> None:
    _add_source(db_path)
    _imported(db_path, "transcript.json")
    with connect(db_path) as db:
        db.execute("UPDATE sources SET speech_status = ? WHERE id = 7", (status,))
    transcribe = _stage_lines(_detail_page(db_path))["Transcribe"]
    assert "Failed" in transcribe
    for absent in ("Retry", "Rerun", "<a", "<button"):
        assert absent not in transcribe


def test_failed_ingest_detail_names_the_terminal_step(db_path: Path) -> None:
    _add_source(db_path)
    with connect(db_path) as db:
        db.execute("UPDATE sources SET speech_status = 'failed' WHERE id = 7")
    lines = _stage_lines(_detail_page(db_path))
    assert "Failed" in lines["Ingest"]
    assert "with montaj ingest, then refresh." in lines["Ingest"]
    assert "Blocked" in lines["Transcribe"] and "Ingest first" in lines["Transcribe"]


def test_batch_confirm_page_posts_a_confirmed_resume(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_source(db_path)
    settings = Settings(data_dir=db_path.parent)
    jobs = JobQueue(settings)
    seen: list[str] = []

    def fake_estimate(
        settings: Settings, source: int, until: str, language: str, budget: Budget
    ) -> list[str]:
        seen.append(until)
        return ["transcribe: $0.120000 for one full audio pass <i>x</i>"]

    monkeypatch.setattr("montaj.dashboard_jobs.estimate_run", fake_estimate)
    status, body = _confirm(settings, jobs, 7, "stage=batch")
    page = body.decode()
    assert status == 200 and seen == ["draft"]
    assert "transcribe: $0.120000" in page and "&lt;i&gt;x&lt;/i&gt;" in page
    assert "No offline estimate for candidates, rank and draft" in page
    assert 'action="/dashboard/jobs" method="post"' in page
    for field in (
        f'name="token" value="{jobs.token}"',
        'name="resume" value="7"',
        'name="top" value="10"',
        'name="confirm" value="yes"',
    ):
        assert field in page
    assert 'name="stage"' not in page and page.count("<button") == 1
    assert 'href="/dashboard/sources/7">Cancel' in page
    assert _confirm(settings, None, 7, "stage=batch")[0] == 404


def test_batch_confirm_hides_a_failed_estimate(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_source(db_path)
    settings = Settings(data_dir=db_path.parent)

    def failing(*_: object, **__: object) -> list[str]:
        raise ValueError("raw /secret/path")

    monkeypatch.setattr("montaj.dashboard_jobs.estimate_run", failing)
    status, body = _confirm(settings, JobQueue(settings), 7, "stage=batch")
    assert status == 200 and b"Estimate unavailable" in body
    assert b"<form" not in body and b"raw" not in body
