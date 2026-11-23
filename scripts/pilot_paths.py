"""Constrain CLI artifact paths to the operator's working directory."""

from pathlib import Path


def workspace_path(path: Path) -> Path:
    path = path.resolve()
    if not path.is_relative_to(Path.cwd().resolve()):
        raise ValueError("Use a path inside the working directory.")
    return path
