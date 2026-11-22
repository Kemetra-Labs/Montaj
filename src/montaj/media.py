"""Local media tool invocations shared by the pipeline."""

import hashlib
import shutil
import subprocess
from pathlib import Path


class MediaError(RuntimeError):
    """A local media tool could not complete its task."""


def file_hash(path: Path) -> str:
    with path.open("rb") as media:
        return hashlib.file_digest(media, "sha256").hexdigest()


def run_tool(tool: str, args: list[str], *, cwd: Path | None = None) -> str:
    executable = shutil.which(tool)
    if executable is None:
        raise MediaError(f"Install {tool} and add it to PATH.")
    try:
        result = subprocess.run(
            [executable, *args], cwd=cwd, capture_output=True, timeout=1800, check=True
        )
    except subprocess.CalledProcessError as exc:
        raise MediaError(f"{tool} failed with exit code {exc.returncode}.") from None
    except subprocess.TimeoutExpired:
        raise MediaError(f"{tool} timed out.") from None
    except OSError as exc:
        raise MediaError(f"Could not start {tool}.") from exc
    return result.stdout.decode("utf-8", errors="replace")
