import shutil
import subprocess
from pathlib import Path

import pytest

ARABIC_SUBTITLE = """[Script Info]
ScriptType: v4.00+
PlayResX: 320
PlayResY: 180

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Noto Sans Arabic,28,&H00FFFFFF,&H00000000,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,5,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,مرحبا بالعالم
"""


@pytest.mark.media
def test_ffmpeg_draws_arabic_subtitles(ffmpeg: str, tmp_path: Path) -> None:
    font_dir = tmp_path / "fonts"
    font_dir.mkdir()
    font = Path(__file__).parents[1] / "assets/fonts/NotoSansArabic-Regular.ttf"
    shutil.copyfile(font, font_dir / font.name)
    (tmp_path / "fixture.ass").write_text(ARABIC_SUBTITLE, encoding="utf-8")

    result = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=black:s=320x180:d=1",
            "-vf",
            "ass=fixture.ass:fontsdir=fonts",
            "-frames:v",
            "1",
            "-threads:v",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "gray",
            "-",
        ],
        cwd=tmp_path,
        capture_output=True,
        check=True,
    )

    assert len(result.stdout) == 320 * 180
    assert max(result.stdout) - min(result.stdout) > 100
