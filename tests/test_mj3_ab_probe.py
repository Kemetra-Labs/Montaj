import subprocess
from pathlib import Path

import pytest

from montaj.captions import load_caption_style
from montaj.transcribe import Word
from scripts.mj3_ab_probe import (
    TRUE_PEAK_CEILING,
    build_parser,
    frame_bounds,
    gap_stats,
    max_linear_target,
    measure,
    measured_only,
    normalize,
    parse_loudnorm,
    variant,
    video_md5,
)

FFMPEG_NOISE = """[Parsed_loudnorm_0 @ 000001] some log line
{
\t"input_i" : "-30.02",
\t"input_tp" : "-12.38",
\t"input_lra" : "5.10",
\t"input_thresh" : "-40.54",
\t"output_i" : "-20.26",
\t"normalization_type" : "dynamic",
\t"target_offset" : "0.26"
}
"""


def test_loudnorm_json_is_parsed_from_ffmpeg_noise() -> None:
    stats = parse_loudnorm(FFMPEG_NOISE)
    assert stats["input_i"] == -30.02 and stats["target_offset"] == 0.26
    assert stats["normalization_type"] == "dynamic"
    assert set(measured_only(stats)) == {
        "input_i",
        "input_tp",
        "input_lra",
        "input_thresh",
    }
    with pytest.raises(ValueError, match="no loudnorm JSON"):
        parse_loudnorm("nothing here")


def test_linear_target_is_limited_by_the_true_peak_ceiling() -> None:
    # Clip 36 measured -30.02 LUFS and -12.38 dBTP, so constant gain stops near -19.64.
    assert max_linear_target(-30.02, -12.38, TRUE_PEAK_CEILING) == pytest.approx(-19.64)
    # Clip 55 at -38.29 LUFS and -15.90 dBTP cannot reach -20 without a limiter.
    assert max_linear_target(-38.29, -15.90, TRUE_PEAK_CEILING) < -24


def test_frame_bounds_finds_painted_box_or_none() -> None:
    width, height = 8, 6
    frame = bytearray(width * height)
    for row in (2, 3):
        for column in (3, 4, 5):
            frame[row * width + column] = 255
    assert frame_bounds(bytes(frame), width) == (3, 5, 2, 3)
    assert frame_bounds(bytes(width * height), width) is None


def test_gap_stats_counts_blank_time_and_flash_events() -> None:
    words = [
        Word(text="one", start=0.0, end=0.4),
        Word(text="two", start=0.6, end=1.0),
        Word(text="three", start=1.05, end=1.15),
        Word(text="four", start=1.15, end=1.6),
    ]
    stats = gap_stats(words)
    assert stats["gaps"] == 2 and stats["gaps_at_least_0_1s"] == 1
    assert stats["blank_seconds"] == pytest.approx(0.25)
    assert stats["events_under_0_15s"] == 1 and stats["longest_gap"] == 0.2


def test_pilot_style_is_validated_like_the_production_style() -> None:
    base = load_caption_style()
    assert variant(base, size=84, outline=5.0).size == 84
    with pytest.raises(ValueError):
        variant(base, size=200)


@pytest.mark.media
def test_linear_normalization_reaches_target_without_touching_video(
    tmp_path: Path, ffmpeg: str
) -> None:
    source = tmp_path / "quiet.mp4"
    subprocess.run(
        [
            ffmpeg, "-nostdin", "-v", "error", "-f", "lavfi",
            "-i", "testsrc2=size=64x64:rate=10:duration=12",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=12",
            "-af", "volume=0.4,volume=0.4:enable='lt(t,6)'", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k", str(source),
        ],
        check=True,
    )  # fmt: skip
    first = measure(source)
    output = tmp_path / "loud.mp4"
    second = normalize(source, output, first, -20.0, "128k")
    assert second["normalization_type"] == "linear"
    after = measure(output)
    assert after["input_i"] == pytest.approx(-20.0, abs=0.5)
    assert after["input_tp"] <= TRUE_PEAK_CEILING
    assert video_md5(output) == video_md5(source)


def test_cli_paths_must_stay_inside_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    inside = ["--data-dir", "data", "--out-dir", "out", "sweep"]
    assert build_parser().parse_args(inside).out_dir == tmp_path / "out"
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--data-dir", "data", "--out-dir", "..", "sweep"])
