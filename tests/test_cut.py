import json
import shutil

import pytest
from typer.testing import CliRunner

from montaj.cli import app
from montaj.config import Settings
from montaj.cut import cut_source
from montaj.db import connect, fetch, insert, save_clip
from montaj.media import run_tool
from montaj.models import Clip, ClipStatus
from montaj.rank import RankedClip, RankSet


@pytest.fixture
def ranked_clip(prepared_source: Settings) -> Clip:
    with connect(prepared_source.db_path) as db:
        clip = insert(
            db,
            Clip(id=1, source_id=1, start_s=0.5, end_s=2.5, status=ClipStatus.RANKED),
        )
    rank = RankSet(
        input_hash="fixture",
        clips=[
            RankedClip(clip_id=1, score=8, reason="Fixture", selected=True),
        ],
    )
    (prepared_source.data_dir / "sources/1/rank.json").write_text(
        rank.model_dump_json(), encoding="utf-8"
    )
    return clip


@pytest.mark.media
def test_cut_renders_vertical_video_and_reuses_it(
    prepared_source: Settings, ranked_clip: Clip, ffmpeg: str
) -> None:
    if shutil.which("ffprobe") is None:
        pytest.skip("Install FFprobe alongside FFmpeg to inspect cut output.")
    first = CliRunner().invoke(app, ["cut", "1", "--clip", "1"])
    assert first.exit_code == 0, first.output
    output = prepared_source.data_dir / "clips/1/raw.mp4"
    actual = json.loads(
        run_tool(
            "ffprobe",
            [
                "-v",
                "error",
                "-show_entries",
                "stream=codec_name,width,height:format=duration",
                "-of",
                "json",
                str(output),
            ],
        )
    )
    video, audio = actual["streams"]
    assert (video["width"], video["height"], video["codec_name"]) == (
        1080,
        1920,
        "h264",
    )
    assert audio["codec_name"] == "aac"
    assert float(actual["format"]["duration"]) == pytest.approx(2, abs=0.1)
    assert cut_source(prepared_source, 1) == (0, 1)

    with connect(prepared_source.db_path) as db:
        saved = fetch(db, Clip, 1)
        assert saved is not None and saved.status == ClipStatus.RENDERED
        saved.end_s = 2
        save_clip(db, saved)
    assert cut_source(prepared_source, 1) == (1, 0)
