import shutil
import subprocess
from pathlib import Path

import pytest

from montaj.caption_layout import (
    LINGER,
    SAFE_WIDTH,
    TimedWord,
    build_groups,
    caption_events,
    text_width,
)
from montaj.captions import load_caption_style, render_ass
from montaj.config import Settings
from montaj.models import Clip
from montaj.transcribe import Word

STYLE = load_caption_style()
ENGLISH = (
    "Extraordinary opportunities sometimes require uncomfortable conversations with "
    "stakeholders, but transparency consistently outperforms improvisation."
).split()
ARABIC = "الفرص الاستثنائية تتطلب أحيانا محادثات صريحة مع الشركاء والمستثمرين".split()


def _timed(texts: list[str], step: float = 0.4, length: float = 0.3) -> list[TimedWord]:
    return [
        TimedWord(i * step, i * step + length, text) for i, text in enumerate(texts)
    ]


@pytest.mark.parametrize("texts", [ENGLISH, ARABIC])
def test_groups_fit_the_safe_width_in_two_lines(texts: list[str]) -> None:
    words = _timed(texts)
    groups = build_groups(words, STYLE.size, STYLE.outline, STYLE.words_per_line)
    assert [i for g in groups for line in g for i in line] == list(range(len(words)))
    for group in groups:
        assert 1 <= len(group) <= 2
        for line in group:
            assert len(line) <= STYLE.words_per_line
            text = " ".join(words[i].text for i in line)
            assert (
                len(line) == 1
                or text_width(text, STYLE.size, STYLE.outline) <= SAFE_WIDTH
            )


def test_sentence_ends_and_long_pauses_start_a_new_group() -> None:
    words = [
        TimedWord(0.0, 0.3, "We"),
        TimedWord(0.35, 0.6, "won."),
        TimedWord(0.7, 0.9, "Then"),
        TimedWord(1.0, 1.2, "silence"),
        TimedWord(2.5, 2.8, "returned"),
    ]
    groups = build_groups(words, STYLE.size, STYLE.outline, STYLE.words_per_line)
    assert [[i for line in g for i in line] for g in groups] == [[0, 1], [2, 3], [4]]


def test_highlights_bridge_short_gaps_but_not_long_ones() -> None:
    words = [
        TimedWord(0.0, 0.3, "one"),
        TimedWord(0.6, 0.9, "two"),  # 0.3 s gap is bridged
        TimedWord(2.0, 2.3, "three"),  # 1.1 s pause ends the group
    ]
    events = caption_events(
        words, 5.0, size=STYLE.size, outline=STYLE.outline, words_per_line=4
    )
    assert events[0].end == pytest.approx(0.6)
    assert events[1].end == pytest.approx(0.9 + LINGER)  # then the screen clears
    assert events[-1].end == pytest.approx(2.3 + LINGER)
    for first, second in zip(events, events[1:], strict=False):
        assert first.end <= second.start + 1e-9


def test_last_event_never_outlasts_the_clip() -> None:
    events = caption_events(
        [TimedWord(0.0, 0.95, "end")], 1.0, size=80, outline=5, words_per_line=4
    )
    assert events[-1].end == pytest.approx(1.0)


@pytest.mark.media
@pytest.mark.parametrize("texts", [ENGLISH, ARABIC], ids=["english", "arabic"])
def test_libass_renders_inside_the_mobile_safe_area(
    texts: list[str], tmp_path: Path, ffmpeg: str
) -> None:
    np = pytest.importorskip("numpy")
    words = [
        Word(text=text, start=i * 0.4, end=i * 0.4 + 0.3)
        for i, text in enumerate(texts)
    ]
    duration = len(texts) * 0.4 + 0.5
    (tmp_path / "captions.ass").write_text(
        render_ass(words, Clip(source_id=1, start_s=0, end_s=duration), STYLE),
        encoding="utf-8",
    )
    fonts = tmp_path / "fonts"
    fonts.mkdir()
    font = Settings().resolved_font_path
    shutil.copyfile(font, fonts / font.name)
    raw = subprocess.run(
        [
            ffmpeg, "-nostdin", "-v", "error", "-f", "lavfi",
            "-i", f"color=c=black:s=1080x1920:r=5:d={duration}",
            "-vf", "ass=captions.ass:fontsdir=fonts", "-f", "rawvideo", "-pix_fmt", "gray", "-",
        ],
        cwd=tmp_path, check=True, capture_output=True,
    ).stdout  # fmt: skip
    frames = np.frombuffer(raw, np.uint8).reshape(-1, 1920, 1080)
    lit = frames > 60
    assert lit.any(axis=(1, 2)).mean() > 0.8  # text stays on screen
    ys, xs = np.nonzero(lit.any(axis=0))
    assert xs.min() >= 90 and xs.max() <= 990  # inside the side margins
    assert ys.max() <= 1920 - 384  # above the platform controls
    assert ys.min() >= 1100  # lower third, clear of faces
