"""Measure clip loudness and choose a peak-safe constant gain."""

import json
import math
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from montaj.media import MediaError

TRUE_PEAK_CEILING = -2.0
LOUDNESS_RANGE = 11.0
MIN_GAIN_DB = 0.1
_JSON_BLOCK = re.compile(r"\{[^{}]*\}")


@dataclass(frozen=True, slots=True)
class Loudness:
    integrated: float
    true_peak: float
    loudness_range: float


def parse_loudnorm(stderr: str) -> dict[str, float | str]:
    """Return the last JSON block FFmpeg printed, with numeric strings converted."""
    blocks = _JSON_BLOCK.findall(stderr)
    if not blocks:
        raise ValueError("FFmpeg printed no loudnorm JSON.")
    parsed: dict[str, str] = json.loads(blocks[-1])
    return {key: _number(value) for key, value in parsed.items()}


def _number(value: str) -> float | str:
    try:
        return float(value)
    except ValueError:
        return value


def linear_gain_db(
    integrated: float,
    true_peak: float,
    target: float,
    ceiling: float = TRUE_PEAK_CEILING,
) -> float:
    """Constant gain toward the target that keeps the true peak under the ceiling.

    Never attenuates, skips adjustments below MIN_GAIN_DB and leaves silence alone.
    """
    if not (math.isfinite(integrated) and math.isfinite(true_peak)):
        return 0.0
    gain = min(target - integrated, ceiling - true_peak)
    return round(gain, 2) if gain >= MIN_GAIN_DB else 0.0


def measure_loudness(
    path: Path, *, start: float | None = None, duration: float | None = None
) -> Loudness:
    """Measure the first audio stream, optionally only a trimmed segment of it."""
    executable = shutil.which("ffmpeg")
    if executable is None:
        raise MediaError("Install ffmpeg and add it to PATH.")
    trim = [] if start is None else ["-ss", str(start)]
    limit = [] if duration is None else ["-t", str(duration)]
    audio_filter = (
        f"loudnorm=I=-20:TP={TRUE_PEAK_CEILING}:LRA={LOUDNESS_RANGE}:print_format=json"
    )
    command = [
        executable, "-nostdin", "-hide_banner", "-nostats", "-protocol_whitelist", "file,pipe",
        *trim, "-i", str(path), *limit, "-map", "0:a:0", "-af", audio_filter, "-f", "null", "-",
    ]  # fmt: skip
    try:
        run = subprocess.run(
            command, capture_output=True, text=True, errors="replace", timeout=1800
        )
    except subprocess.TimeoutExpired:
        raise MediaError("ffmpeg timed out while measuring loudness.") from None
    if run.returncode:
        raise MediaError(f"ffmpeg could not measure loudness: {run.stderr[-300:]}")
    try:
        stats = parse_loudnorm(run.stderr)
        return Loudness(
            float(stats["input_i"]),
            float(stats["input_tp"]),
            float(stats["input_lra"]),
        )
    except (KeyError, ValueError) as exc:
        raise MediaError("ffmpeg returned no usable loudness values.") from exc
