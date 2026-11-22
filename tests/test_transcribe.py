import importlib
import json
import shutil
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request

import pytest
from typer.testing import CliRunner

from montaj.budget import Budget
from montaj.cli import app
from montaj.config import Settings
from montaj.db import connect, insert
from montaj.models import Niche, Source
from montaj.transcribe import Transcript, Word, transcribe_source

module = importlib.import_module("montaj.transcribe")


@pytest.fixture
def prepared_source(db_path: Path, media_fixture_dir: Path) -> Settings:
    settings = Settings(data_dir=db_path.parent)
    with connect(db_path) as db:
        insert(
            db,
            Source(
                id=1,
                path="source.mp4",
                title="Fixture",
                niche=Niche.GAMES,
                source_allowed=True,
            ),
        )
    directory = db_path.parent / "sources/1"
    directory.mkdir(parents=True)
    shutil.copyfile(media_fixture_dir / "sample.mp4", directory / "video.mp4")
    shutil.copyfile(media_fixture_dir / "sample.wav", directory / "audio.wav")
    (directory / "meta.json").write_text(
        '{"title":"Fixture","duration":18,"video_hash":"video","audio_hash":"audio"}',
        encoding="utf-8",
    )
    return settings


def test_deepgram_words_query_and_cache(
    prepared_source: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPGRAM_API_KEY", "test-token")
    requests: list[Request] = []

    def respond(request: Request, *, timeout: int) -> BytesIO:
        requests.append(request)
        channel = {
            "alternatives": [
                {
                    "transcript": "Hello world.",
                    "words": [
                        {
                            "word": "hello",
                            "punctuated_word": "Hello",
                            "start": 0.1,
                            "end": 0.4,
                            "speaker": 0,
                        },
                        {
                            "word": "world",
                            "punctuated_word": "world.",
                            "start": 0.5,
                            "end": 0.8,
                        },
                    ],
                }
            ]
        }
        return BytesIO(
            json.dumps(
                {"metadata": {"duration": 18}, "results": {"channels": [channel]}}
            ).encode()
        )

    monkeypatch.setattr(module, "urlopen", respond)
    runner = CliRunner()
    args = ["transcribe", "1", "--language", "ar-EG", "--budget", "0.1"]
    first = runner.invoke(app, args)
    assert first.exit_code == 0, first.output
    cached = runner.invoke(app, args)
    assert cached.exit_code == 0 and "reused" in cached.output
    assert len(requests) == 1
    query = parse_qs(urlsplit(requests[0].full_url).query)
    assert query["model"] == ["nova-3"] and query["language"] == ["ar-EG"]
    transcript = Transcript.model_validate_json(
        (prepared_source.data_dir / "sources/1/transcript.json").read_bytes()
    )
    assert transcript.language == "ar-EG"
    assert [(word.text, word.speaker) for word in transcript.words] == [
        ("Hello", 0),
        ("world.", None),
    ]


def test_budget_flag_blocks_transcription_before_request(
    prepared_source: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPGRAM_API_KEY", "test-token")
    result = CliRunner().invoke(app, ["transcribe", "1", "--budget", "0"])
    assert result.exit_code == 1 and "budget ceiling" in result.output
    with connect(prepared_source.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM api_calls").fetchone()[0] == 0


def test_chunks_keep_offsets_and_resume_completed_parts(
    prepared_source: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(module, "MAX_REQUEST_BYTES", 1)
    monkeypatch.setattr(module, "CHUNK_SECONDS", 7)

    class Stub:
        calls = 0

        def transcribe(self, path: Path) -> Transcript:
            self.calls += 1
            if self.calls == 2:
                raise TimeoutError("temporary test failure")
            return Transcript(
                text="Word",
                language="en",
                words=[Word(text="Word", start=0.5, end=0.8)],
            )

    stub = Stub()
    with pytest.raises(TimeoutError, match="test failure"):
        transcribe_source(
            prepared_source, 1, language="en", budget=Budget(1, 1), backend=stub
        )
    result, reused = transcribe_source(
        prepared_source, 1, language="en", budget=Budget(1, 1), backend=stub
    )
    assert not reused
    assert [word.start for word in result.words] == [0.5, 7.5, 14.5]
    assert stub.calls == 4
