import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from montaj import dashboard_jobs, job, media
from montaj.board_view import LockInfo, job_problem_note, source_locks
from montaj.config import Settings
from montaj.dashboard_parts import source_problem

_GENERIC = "The job stopped. See the dashboard terminal for the details."
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
        ("This source is already being processed by <b> since now.", _GENERIC),
        ("", ""),
        ("   ", ""),
    ],
)
def test_job_problem_note_rewords_the_rest(problem: str, note: str) -> None:
    assert job_problem_note(problem) == note
