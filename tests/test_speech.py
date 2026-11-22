from pathlib import Path

import pytest

from montaj.budget import Budget
from montaj.candidates import CandidateBounds, find_candidates
from montaj.config import Settings
from montaj.db import connect, fetch
from montaj.models import Source
from montaj.transcribe import (
    SpeechError,
    Transcript,
    Word,
    languages_match,
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
