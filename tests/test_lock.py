import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from montaj.budget import Budget
from montaj.cli import app
from montaj.config import Settings
from montaj.media import MediaError, source_lock
from montaj.run import run_source
from montaj.transcribe import Transcript, Word

_CHILD = """
import sys
import time
from pathlib import Path

from montaj.media import source_lock

directory = Path(sys.argv[1])
seconds = float(sys.argv[2])
with source_lock(directory, command="transcribe"):
    time.sleep(seconds)
"""


def _hold(directory: Path, seconds: float) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", _CHILD, str(directory), str(seconds)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _await_holder(process: subprocess.Popen[str], holder: Path) -> None:
    deadline = time.monotonic() + 5
    while not holder.is_file():
        if process.poll() is not None:
            out, err = process.communicate()
            raise AssertionError(err or out or "lock holder exited early")
        if time.monotonic() > deadline:
            process.kill()
            raise AssertionError("timed out waiting for the lock holder")
        time.sleep(0.02)


def _stop(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.kill()
        process.wait(timeout=5)


def test_second_command_is_refused_and_wait_runs_after_release(tmp_path: Path) -> None:
    directory = tmp_path / "sources" / "1"
    process = _hold(directory, 0.8)
    try:
        _await_holder(process, directory / ".lock-holder")
        with pytest.raises(MediaError, match=r"by transcribe since \d{4}-\d{2}-\d{2}T"):
            with source_lock(directory, command="rank", wait=0):
                pass
        with source_lock(directory, command="rank", wait=3):
            assert (directory / ".lock").is_file()
    finally:
        _stop(process)
    assert process.wait(timeout=5) is not None


def test_killed_process_frees_the_lock(tmp_path: Path) -> None:
    directory = tmp_path / "sources" / "1"
    process = _hold(directory, 30)
    try:
        _await_holder(process, directory / ".lock-holder")
        process.kill()
        process.wait(timeout=5)
        with source_lock(directory, command="rank", wait=2):
            assert (
                (directory / ".lock-holder")
                .read_text(encoding="utf-8")
                .startswith('{"command": "rank"')
            )
    finally:
        _stop(process)


def test_same_process_reenters_the_lock(tmp_path: Path) -> None:
    directory = tmp_path / "sources" / "1"
    with source_lock(directory, command="run"):
        with source_lock(directory, command="transcribe"):
            holder = (directory / ".lock-holder").read_text(encoding="utf-8")
        assert '"command": "run"' in holder
    assert not (directory / ".lock-holder").exists()


def test_run_calling_stages_does_not_deadlock(prepared_source: Settings) -> None:
    class Backend:
        def transcribe(self, audio_path: Path) -> Transcript:
            del audio_path
            return Transcript(
                text="mot",
                language="en",
                words=[Word(text="mot", start=0, end=0.5)],
            )

    finished = threading.Event()
    errors: list[BaseException] = []

    def work() -> None:
        try:
            run_source(
                prepared_source,
                1,
                until="transcribe",
                budget=Budget(1, 1),
                backend=Backend(),
            )
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    thread = threading.Thread(target=work)
    thread.start()
    assert finished.wait(5), "run deadlocked on the source lock"
    thread.join(timeout=1)
    assert errors == []


def test_read_only_commands_do_not_lock(db_path: Path) -> None:
    runner = CliRunner()
    for args in (
        ["costs"],
        ["doctor"],
        ["account", "list"],
        ["campaign", "list"],
        ["post-log", "list"],
        ["run", "--help"],
    ):
        runner.invoke(app, args)
    assert list(db_path.parent.rglob(".lock")) == []
    help_text = runner.invoke(app, ["transcribe", "--help"])
    assert help_text.exit_code == 0
    assert "--wait" in help_text.output
