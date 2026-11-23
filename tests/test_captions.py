from pathlib import Path

import pytest

from montaj.captions import caption_source, load_caption_style, render_ass
from montaj.config import Settings
from montaj.cut import cut_source
from montaj.db import connect, fetch
from montaj.models import Clip, ClipStatus
from montaj.transcribe import Transcript, Word


def test_ass_highlights_each_word_at_clip_relative_time() -> None:
    clip = Clip(source_id=1, start_s=10, end_s=15)
    words = [
        Word(text=f"word{index}", start=10 + index / 2, end=10.4 + index / 2)
        for index in range(9)
    ]
    output = render_ass(words, clip, load_caption_style())
    events = [line for line in output.splitlines() if line.startswith("Dialogue:")]
    assert len(events) == 9
    # Short gaps between words are bridged so the caption never blinks off.
    assert events[0].startswith("Dialogue: 0,0:00:00.00,0:00:00.50,")
    assert events[1].startswith("Dialogue: 0,0:00:00.50,0:00:01.00,")
    # The last word of the clip lingers briefly after it is spoken.
    assert events[-1].startswith("Dialogue: 0,0:00:04.00,0:00:04.70,")
    assert r"{\c&H52DCFF&}word0" in events[0]
    assert events[0].count(r"\N") == 1
    assert r"\N" not in events[-1]


@pytest.mark.media
def test_cut_and_burn_arabic_captions(
    prepared_source: Settings,
    ranked_clip: Clip,
    ffmpeg: str,
) -> None:
    directory = prepared_source.data_dir / "sources/1"
    transcript = Transcript(
        text="مرحبا بالعالم",
        language="ar-EG",
        words=[
            Word(text="مرحبا", start=0.5, end=1.25),
            Word(text="بالعالم", start=1.25, end=2.3),
        ],
    )
    (directory / "transcript.json").write_text(
        transcript.model_dump_json(), encoding="utf-8"
    )
    assert cut_source(prepared_source, 1) == (1, 0)
    assert caption_source(prepared_source, 1) == (1, 0)
    assert caption_source(prepared_source, 1) == (0, 1)
    output = prepared_source.data_dir / "clips/1"
    assert (output / "final.mp4").stat().st_size > 0
    ass = (output / "captions.ass").read_text(encoding="utf-8")
    assert "مرحبا" in ass and "بالعالم" in ass
    with connect(prepared_source.db_path) as db:
        saved = fetch(db, Clip, 1)
        assert saved is not None and saved.status == ClipStatus.CAPTIONED
        assert saved.final_path == Path("clips/1/final.mp4").as_posix()
