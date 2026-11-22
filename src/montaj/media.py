"""Local media tool invocations shared by the pipeline."""

import hashlib
import shutil
import sqlite3
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile


class MediaError(RuntimeError):
    """A local media tool could not complete its task."""


def file_hash(path: Path) -> str:
    with path.open("rb") as media:
        return hashlib.file_digest(media, "sha256").hexdigest()


def atomic_text(path: Path, content: str) -> None:
    temporary: Path | None = None
    try:
        with NamedTemporaryFile(dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            output.write(content.encode("utf-8"))
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@contextmanager
def source_lock(directory: Path) -> Iterator[None]:
    """Serialize source artifact writes without locking the main database."""
    connection = sqlite3.connect(directory / ".pipeline-lock.db", timeout=0)
    try:
        try:
            connection.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            raise MediaError(
                "This source is already being processed or cannot be locked."
            ) from exc
        yield
    finally:
        connection.close()


def run_tool(tool: str, args: list[str], *, cwd: Path | None = None) -> str:
    executable = shutil.which(tool)
    if executable is None:
        raise MediaError(f"Install {tool} and add it to PATH.")
    try:
        result = subprocess.run(
            [executable, *args], cwd=cwd, capture_output=True, timeout=1800, check=True
        )
    except subprocess.CalledProcessError as exc:
        detail = ""
        if tool in {"ffmpeg", "ffprobe"} and exc.stderr:
            detail = exc.stderr.decode("utf-8", errors="replace").strip()[-400:]
        message = f"{tool} failed with exit code {exc.returncode}."
        raise MediaError(f"{message} {detail}".strip()) from None
    except subprocess.TimeoutExpired:
        raise MediaError(f"{tool} timed out.") from None
    except OSError as exc:
        raise MediaError(f"Could not start {tool}.") from exc
    return result.stdout.decode("utf-8", errors="replace")
