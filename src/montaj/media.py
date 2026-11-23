"""Local media tool invocations shared by the pipeline."""

import errno
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import get_ident


class MediaError(RuntimeError):
    """A local media tool could not complete its task."""


@dataclass(slots=True)
class _FileHashMemo:
    path: Path
    stamp: tuple[int, int, int, int] | None = None
    digest: str = ""


_file_hash_memo: ContextVar[_FileHashMemo | None] = ContextVar(
    "file_hash_memo", default=None
)


@contextmanager
def reuse_file_hash(path: Path) -> Iterator[None]:
    """Share the source video's digest across stages in one locked batch."""
    token = _file_hash_memo.set(_FileHashMemo(path))
    try:
        yield
    finally:
        _file_hash_memo.reset(token)


def file_hash(path: Path) -> str:
    memo = _file_hash_memo.get()
    with path.open("rb") as media:
        if memo is None or path != memo.path:
            return hashlib.file_digest(media, "sha256").hexdigest()
        stat = os.fstat(media.fileno())
        stamp = (stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        if stamp != memo.stamp:
            memo.digest = hashlib.file_digest(media, "sha256").hexdigest()
            memo.stamp = stamp
        return memo.digest


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


def atomic_bytes(path: Path, content: bytes) -> None:
    temporary: Path | None = None
    try:
        with NamedTemporaryFile(dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            output.write(content)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


# Stage calls may nest within one job. Concurrent threads must acquire the OS lock.
_held_depth: dict[tuple[int, str], int] = {}


@contextmanager
def source_lock(
    directory: Path, *, command: str = "pipeline", wait: float = 0
) -> Iterator[None]:
    """Lock data/sources/<id>/.lock. The OS drops it when this process exits."""
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / ".lock"
    key = (get_ident(), str(lock_path.resolve()))
    if _held_depth.get(key, 0) > 0:
        _held_depth[key] += 1
        try:
            yield
        finally:
            _held_depth[key] -= 1
        return
    fd = _acquire(lock_path, max(wait, 0))
    holder = directory / ".lock-holder"
    try:
        holder.write_text(_holder_text(command), encoding="utf-8")
    except OSError as exc:
        _release(fd)
        raise MediaError(
            "This source is already being processed or cannot be locked."
        ) from exc
    _held_depth[key] = 1
    try:
        yield
    finally:
        _held_depth.pop(key, None)
        holder.unlink(missing_ok=True)
        _release(fd)


def _holder_text(command: str) -> str:
    started = datetime.now(UTC).isoformat(timespec="seconds")
    return json.dumps({"command": command, "started": started}) + "\n"


def _acquire(lock_path: Path, wait: float) -> int:
    deadline = time.monotonic() + wait
    while True:
        try:
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        except OSError as exc:
            raise MediaError(
                "This source is already being processed or cannot be locked."
            ) from exc
        try:
            _ensure_byte(fd)
            _try_lock(fd)
        except BlockingIOError:
            os.close(fd)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MediaError(_busy_message(lock_path)) from None
            time.sleep(min(0.05, remaining))
            continue
        except OSError as exc:
            os.close(fd)
            raise MediaError(
                "This source is already being processed or cannot be locked."
            ) from exc
        return fd


def _ensure_byte(fd: int) -> None:
    """Windows locks a byte range, so the file has to contain that byte."""
    if os.lseek(fd, 0, os.SEEK_END) < 1:
        os.write(fd, b"\0")


def _try_lock(fd: int) -> None:
    os.lseek(fd, 0, os.SEEK_SET)
    if sys.platform == "win32":
        import msvcrt

        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise BlockingIOError(exc.errno or 0, "locked") from exc
        return
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in {errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES}:
            raise BlockingIOError(exc.errno or 0, "locked") from exc
        raise


def _release(fd: int) -> None:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    os.close(fd)


def _busy_message(lock_path: Path) -> str:
    fallback = "This source is already being processed or cannot be locked."
    try:
        payload = json.loads(
            (lock_path.parent / ".lock-holder").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError):
        return fallback
    if not isinstance(payload, dict):
        return fallback
    command = payload.get("command")
    started = payload.get("started")
    if isinstance(command, str) and command and isinstance(started, str) and started:
        return f"This source is already being processed by {command} since {started}."
    return fallback


def run_tool(
    tool: str,
    args: list[str],
    *,
    cwd: Path | None = None,
    error_message: Callable[[str], str] | None = None,
) -> str:
    executable = shutil.which(tool)
    if executable is None:
        raise MediaError(f"Install {tool} and add it to PATH.")
    try:
        result = subprocess.run(
            [executable, *args], cwd=cwd, capture_output=True, timeout=1800, check=True
        )
    except subprocess.CalledProcessError as exc:
        if error_message is not None:
            stderr = (exc.stderr or b"").decode("utf-8", errors="replace")
            raise MediaError(error_message(stderr)) from None
        detail = ""
        if tool in {"ffmpeg", "ffprobe"} and exc.stderr:
            detail = exc.stderr.decode("utf-8", errors="replace").strip()[-400:]
        message = f"{tool} failed with exit code {exc.returncode}."
        raise MediaError(f"{message} {detail}".strip()) from None
    except subprocess.TimeoutExpired:
        message = f"{tool} timed out."
        raise MediaError(error_message(message) if error_message else message) from None
    except OSError as exc:
        raise MediaError(f"Could not start {tool}.") from exc
    return result.stdout.decode("utf-8", errors="replace")
