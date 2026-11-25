import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest

from montaj import dashboard_jobs, job, media
from montaj.board_view import LockInfo, job_problem_note, source_locks
from montaj.config import Settings
from montaj.dashboard_parts import source_problem

_GENERIC = "The job stopped. The details are in montaj.log in the logs folder of the data directory."
_RUN_STOP = "The run ceiling was reached before the next paid call."
_SOURCE_STOP = "The source ceiling was reached before the next paid call."


def _holder(settings: Settings, source_id: int, text: str) -> Path:
    directory = settings.data_dir / "sources" / str(source_id)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ".lock-holder"
    path.write_text(text, encoding="utf-8")
    return path


def test_source_locks_reads_the_holder(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path)
    _holder(
        settings,
        7,
        json.dumps({"command": "pipeline", "started": "2026-11-25T10:00:00+00:00"}),
    )
    locks = source_locks(settings, [7, 8], None)
    assert locks == {7: LockInfo("pipeline", datetime(2026, 11, 25, 10, tzinfo=UTC))}


def test_source_locks_reads_what_source_lock_writes(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path)
    directory = settings.data_dir / "sources" / "7"
    with media.source_lock(directory, command="rank"):
        lock = source_locks(settings, [7], None)[7]
    assert lock.command == "rank" and lock.started is not None
    assert source_locks(settings, [7], None) == {}


def test_source_locks_skips_the_running_source(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path)
    _holder(settings, 7, json.dumps({"command": "pipeline", "started": "x"}))
    assert source_locks(settings, [7], (7, "batch")) == {}
    assert 7 in source_locks(settings, [7], (8, "batch"))


@pytest.mark.parametrize(
    ("text", "lock"),
    [
        ("", LockInfo("", None)),
        ("not json", LockInfo("", None)),
        ("[1, 2]", LockInfo("", None)),
        ("{}", LockInfo("", None)),
        (json.dumps({"command": "rank", "started": "soon"}), LockInfo("rank", None)),
        (json.dumps({"command": 5, "started": None}), LockInfo("", None)),
        (json.dumps({"command": "<b>x</b>"}), LockInfo("", None)),
        ("\xff\xfe", LockInfo("", None)),
    ],
)
def test_source_locks_survives_bad_files(
    tmp_path: Path, text: str, lock: LockInfo
) -> None:
    settings = Settings(data_dir=tmp_path)
    _holder(settings, 7, text)
    assert source_locks(settings, [7], None) == {7: lock}


def test_source_locks_takes_a_naive_time_as_utc(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path)
    _holder(settings, 7, json.dumps({"command": "cut", "started": "2026-11-25T10:00"}))
    started = source_locks(settings, [7], None)[7].started
    assert started == datetime(2026, 11, 25, 10, tzinfo=UTC)


def test_source_locks_does_not_follow_a_symlink(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"command": "secret", "started": "x"}))
    directory = settings.data_dir / "sources" / "7"
    directory.mkdir(parents=True)
    try:
        (directory / ".lock-holder").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks need extra rights here")
    assert source_locks(settings, [7], None) == {7: LockInfo("", None)}


_PASS_THROUGH = [
    dashboard_jobs._UNEXPECTED,
    dashboard_jobs._FILE_PROBLEM,
    dashboard_jobs._MEDIA_PROBLEM,
    job.NO_MORE_CANDIDATES,
    media.BUSY_MESSAGE,
    "This source is already being processed by pipeline since 2026-11-25T10:00:00+00:00.",
    *(
        source_problem(status, note)
        for status, note in (
            ("language_mismatch", None),
            ("no_usable_speech", None),
            ("failed", "javascript runtime"),
            ("failed", "ejs solver"),
            ("failed", "format not available"),
            ("failed", None),
        )
    ),
]


@pytest.mark.parametrize("problem", _PASS_THROUGH)
def test_job_problem_note_keeps_montaj_sentences(problem: str) -> None:
    assert job_problem_note(problem) == problem


@pytest.mark.parametrize(
    ("problem", "note"),
    [
        (
            "rank: per-run budget ceiling $0.500000; estimated spend $0.490000, "
            "next call $0.020000.",
            _RUN_STOP,
        ),
        (
            "draft: per-source budget ceiling $2.000000; estimated spend $1.990000, "
            "next call $0.020000.",
            _SOURCE_STOP,
        ),
        ("C:\\data\\sources\\7\\video.mp4 not found", _GENERIC),
        ("Traceback (most recent call last)", _GENERIC),
        ("<b>x</b>", _GENERIC),
        ("Failed to fetch https://example.com/v?id=1", _GENERIC),
        ("first line\nsecond line", _GENERIC),
        ("x" * 301, _GENERIC),
        ("This source is already being processed by <b> since now.", _GENERIC),
        ("", ""),
        ("   ", ""),
    ],
)
def test_job_problem_note_rewords_the_rest(problem: str, note: str) -> None:
    assert job_problem_note(problem) == note


@pytest.mark.parametrize(
    "problem",
    [
        "Record a permission basis on the source before running a stage.",
        "Set ANTHROPIC_API_KEY before requesting Claude output.",
        "Run rank first.",
        "Ranked clip 3 is missing. Rerun rank.",
    ],
)
def test_job_problem_note_keeps_a_plain_montaj_sentence(problem: str) -> None:
    assert job_problem_note(problem) == problem


@pytest.mark.parametrize(
    "exc",
    [
        ValueError("Record a permission basis on the source before running a stage."),
        media.MediaError("ffmpeg exited with status 1"),
    ],
)
def test_every_job_problem_is_logged(
    exc: Exception, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(logging.getLogger("montaj"), "propagate", True)
    problem = dashboard_jobs._job_problem(exc)
    assert job_problem_note(problem) == str(exc)
    assert str(exc) in caplog.text


def test_the_dashboards_own_import_is_not_locked(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path)
    _holder(settings, 7, json.dumps({"command": "ingest", "started": "x"}))
    assert source_locks(settings, [7], (0, "batch")) == {}
    assert 7 in source_locks(settings, [7], None)
    _holder(settings, 7, json.dumps({"command": "pipeline", "started": "x"}))
    assert 7 in source_locks(settings, [7], (0, "batch"))
