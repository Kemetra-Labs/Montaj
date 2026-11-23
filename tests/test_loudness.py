import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from montaj import render
from montaj.config import Settings
from montaj.cut import CUT_STYLE, FFmpegRenderer, cut_source, cut_style
from montaj.engine import loudness_policy
from montaj.loudness import (
    Loudness,
    LoudnessPolicy,
    backoff_gain_db,
    linear_gain_db,
    measure_loudness,
)
from montaj.models import Clip


class _Writer:
    def render(self, video: Path, output: Path, start: float, end: float) -> None:
        output.write_bytes(b"render")


def test_gain_stops_at_the_target_when_the_peak_has_room() -> None:
    # Clip 36: -30.02 LUFS, -12.38 dBTP. The target needs 10.02 dB and the peak allows 10.38.
    assert linear_gain_db(-30.02, -12.38, -20.0) == pytest.approx(10.02)


def test_gain_stops_at_the_peak_ceiling_before_the_target() -> None:
    # Clip 55: -38.29 LUFS, -15.90 dBTP. The target needs 18.29 dB and the peak allows 13.90.
    assert linear_gain_db(-38.29, -15.90, -20.0) == pytest.approx(13.90)


def test_gain_never_attenuates_or_chases_tiny_adjustments() -> None:
    assert linear_gain_db(-18.0, -3.0, -20.0) == 0.0
    assert linear_gain_db(-20.04, -12.0, -20.0) == 0.0
    assert linear_gain_db(-30.0, -1.0, -20.0) == 0.0
    assert linear_gain_db(float("-inf"), float("-inf"), -20.0) == 0.0


def test_gain_is_capped_so_quiet_rooms_are_not_over_boosted() -> None:
    # A very quiet clip with plenty of headroom would otherwise get +30 dB.
    assert linear_gain_db(-48.0, -40.0, -18.0, -2.5, 12.0) == pytest.approx(12.0)


def test_backoff_reduces_gain_by_the_measured_overshoot() -> None:
    assert backoff_gain_db(10.0, -1.6, -2.5) == pytest.approx(9.1)
    assert backoff_gain_db(0.5, 0.0, -2.5) == 0.0


def test_safe_loudness_policy_is_on_by_default_and_changes_the_cut_style() -> None:
    assert Settings().loudness_target_lufs == -18.0
    assert loudness_policy(Settings()) == LoudnessPolicy(-18.0, -2.5, 12.0)
    assert loudness_policy(Settings(engine="legacy")) is None
    assert cut_style(Settings(engine="legacy")) == CUT_STYLE
    louder = cut_style(Settings(loudness_target_lufs=-20))
    assert louder != CUT_STYLE and louder != cut_style(Settings())
    assert louder != cut_style(Settings(loudness_target_lufs=-21))
    assert cut_style(Settings(loudness_target_lufs=None)) != cut_style(Settings())


def test_loudness_target_is_read_from_the_environment_and_range_checked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MONTAJ_LOUDNESS_TARGET_LUFS", "")
    assert Settings.from_env().loudness_target_lufs == -18.0
    monkeypatch.setenv("MONTAJ_LOUDNESS_TARGET_LUFS", "off")
    assert Settings.from_env().loudness_target_lufs is None
    monkeypatch.setenv("MONTAJ_LOUDNESS_TARGET_LUFS", "-20")
    assert Settings.from_env().loudness_target_lufs == -20.0
    monkeypatch.setenv("MONTAJ_LOUDNESS_TARGET_LUFS", "-5")
    with pytest.raises(ValidationError):
        Settings.from_env()


def test_renderer_backs_off_when_the_encoded_peak_is_too_hot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    readings = iter(
        [
            Loudness(-30.0, -14.0, 5.0),
            Loudness(-18.1, -0.6, 5.0),
            Loudness(-19.0, -2.6, 5.0),
        ]
    )
    filters: list[str] = []

    def fake_tool(tool: str, args: list[str], **_: object) -> str:
        filters.append(args[args.index("-af") + 1])
        return ""

    monkeypatch.setattr(render, "measure_loudness", lambda *a, **k: next(readings))
    monkeypatch.setattr(render, "run_tool", fake_tool)
    report = FFmpegRenderer(LoudnessPolicy(-18.0, -2.5, 12.0)).render(
        tmp_path / "in.mp4", tmp_path / "out.mp4", 1.0, 11.0
    )
    assert [f.split(",")[0] for f in filters] == ["volume=11.50dB", "volume=9.60dB"]
    assert report.loudness["backed_off_from_db"] == 11.5
    assert report.loudness["after_dbtp"] == -2.6
    assert report.engine == "center" and report.reason == "legacy_engine"


def test_changing_the_loudness_target_rerenders_but_the_default_stays_cached(
    prepared_source: Settings, ranked_clip: Clip
) -> None:
    assert cut_source(prepared_source, 1, renderer=_Writer()) == (1, 0)
    assert cut_source(prepared_source, 1, renderer=_Writer()) == (0, 1)
    louder = prepared_source.model_copy(update={"loudness_target_lufs": -20.0})
    assert cut_source(prepared_source, 1, renderer=_Writer()) == (0, 1)
    assert cut_source(louder, 1, renderer=_Writer()) == (1, 0)
    assert cut_source(louder, 1, renderer=_Writer()) == (0, 1)


@pytest.mark.media
def test_renderer_applies_clip_gain_in_its_single_audio_encode(
    tmp_path: Path, ffmpeg: str
) -> None:
    source = tmp_path / "quiet.mp4"
    subprocess.run(
        [
            ffmpeg, "-nostdin", "-v", "error", "-f", "lavfi",
            "-i", "testsrc2=size=160x90:rate=10:duration=12",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=12",
            "-af", "volume=0.4,volume=0.4:enable='lt(t,6)'", "-c:v", "libx264",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", str(source),
        ],
        check=True,
    )  # fmt: skip
    plain, louder = tmp_path / "plain.mp4", tmp_path / "louder.mp4"
    FFmpegRenderer().render(source, plain, 1.0, 11.0)
    report = FFmpegRenderer(LoudnessPolicy(-20.0, -2.0)).render(
        source, louder, 1.0, 11.0
    )
    before, after = measure_loudness(plain), measure_loudness(louder)
    assert after.integrated == pytest.approx(-20.0, abs=0.7)
    assert after.true_peak <= -2.0
    assert report.loudness["after_lufs"] == pytest.approx(after.integrated, abs=0.05)
    assert float(report.loudness["gain_db"] or 0) > 5
    assert after.integrated - before.integrated > 5
    assert after.loudness_range == pytest.approx(before.loudness_range, abs=0.6)
