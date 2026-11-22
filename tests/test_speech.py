import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from montaj.budget import Budget
from montaj.candidates import CandidateBounds, find_candidates
from montaj.cli import app
from montaj.config import Settings
from montaj.db import connect, fetch, insert
from montaj.ingest import SourceError, download_failure, enforce_limits, ingest_video
from montaj.media import MediaError
from montaj.models import Campaign, CampaignRules, Niche, Platform, Source
from montaj.transcribe import (
    SpeechError,
    Transcript,
    Word,
    assess_speech,
    languages_match,
    require_usable_speech,
    transcribe_source,
)
from tests.fakes import FakeLLM


class _Backend:
    def __init__(self, transcript: Transcript) -> None:
        self.transcript = transcript
        self.calls = 0

    def transcribe(self, audio_path: Path) -> Transcript:
        del audio_path
        self.calls += 1
        return self.transcript


def test_language_family_matches_and_multi_accepts_anything() -> None:
    assert languages_match("en", "en-US")
    assert languages_match("ar", "ar-EG")
    assert not languages_match("en", "ar-EG")
    assert languages_match("multi", "fr")


def test_thin_speech_is_reported_before_a_model_call() -> None:
    empty = Transcript(text=" ", language="en", words=[])
    assert assess_speech(empty, 5) == "The transcript is empty."
    quiet = Transcript(
        text="hi",
        language="en",
        words=[Word(text="hi", start=0, end=0.2, confidence=0.2)],
    )
    assert "confidence" in (assess_speech(quiet, 5) or "")
    sparse = Transcript(
        text="hi",
        language="en",
        words=[Word(text="hi", start=0, end=0.2)],
    )
    assert "words per minute" in (assess_speech(sparse, 60) or "")


def test_mismatch_stops_before_candidates_and_force_continues(
    prepared_source: Settings,
) -> None:
    backend = _Backend(
        Transcript(
            text="Bonjour",
            language="fr",
            words=[Word(text="Bonjour", start=0.1, end=0.4)],
        )
    )
    with pytest.raises(SpeechError, match="Requested en, detected fr") as mismatch:
        transcribe_source(
            prepared_source,
            1,
            language="en",
            budget=Budget(1, 1),
            backend=backend,
        )
    assert mismatch.value.code == 7
    with pytest.raises(SpeechError, match="different language"):
        find_candidates(
            prepared_source,
            1,
            bounds=CandidateBounds(min_seconds=1, max_seconds=10),
            budget=Budget(1, 1),
            llm=FakeLLM([]),
        )
    forced, reused = transcribe_source(
        prepared_source,
        1,
        language="en",
        budget=Budget(1, 1),
        backend=backend,
        force_language=True,
    )
    assert reused and forced.language == "fr" and backend.calls == 1
    with connect(prepared_source.db_path) as db:
        source = fetch(db, Source, 1)
    assert source is not None and source.speech_status == "ok"
    assert source.requested_language == "en"


def test_limits_download_errors_and_permission_basis(
    db_path: Path, tmp_path: Path
) -> None:
    with pytest.raises(SourceError, match="9.0 minutes") as limited:
        enforce_limits("long.mp4", 540, 10, Settings(max_source_minutes=5))
    assert limited.value.code == 3
    with pytest.raises(SourceError, match="private") as private:
        raise download_failure("ERROR: Private video. Sign in")
    assert private.value.code == 4

    settings = Settings(data_dir=db_path.parent)
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"not a video")
    with pytest.raises(SourceError, match="--basis"):
        ingest_video(settings, str(video), Niche.GAMES, allowed=True)
    rules = CampaignRules(
        min_seconds=1, max_seconds=60, allowed_sources=["allowed.example"]
    )
    with connect(db_path) as db:
        campaign = insert(
            db,
            Campaign(
                name="Limited",
                platform=Platform.YOUTUBE,
                rules_json=rules.model_dump_json(),
            ),
        )
    assert campaign.id is not None
    with pytest.raises(SourceError, match="allowed-source") as refused:
        ingest_video(
            settings,
            "https://other.example/video",
            Niche.GAMES,
            allowed=True,
            basis="campaign allowed-source list",
            campaign_id=campaign.id,
        )
    assert refused.value.code == 2
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "ingest",
            str(video),
            "--niche",
            "games",
            "--allowed",
            "--basis",
            "creator written permission",
        ],
    )
    assert result.exit_code == 5
    assert "could not be read" in result.output
    with connect(db_path) as db:
        failed = fetch(db, Source, 1)
    assert failed is not None
    assert failed.failure_note
    assert failed.allowed_basis == "creator written permission"
    blocked = failed.model_copy(update={"speech_status": "no_usable_speech"})
    with pytest.raises(SpeechError, match="no usable speech"):
        require_usable_speech(blocked)


def test_downloader_error_is_stored(db_path: Path) -> None:
    settings = Settings(data_dir=db_path.parent)

    class Broken:
        def download(self, url: str, destination: Path) -> None:
            del url, destination
            raise MediaError("yt-dlp failed. ERROR: This video is unavailable")

    with pytest.raises(SourceError, match="removed or is unavailable") as failure:
        ingest_video(
            settings,
            "https://example.test/gone",
            Niche.GAMES,
            allowed=True,
            basis="campaign allowed-source list",
            downloader=Broken(),
        )
    assert failure.value.code == 4
    with connect(db_path) as db:
        source = fetch(db, Source, 1)
    assert source is not None and source.speech_status == "failed"
    assert json.dumps(source.failure_note)
