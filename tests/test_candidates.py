import json
from io import BytesIO
from urllib.request import Request

import pytest
from pydantic import SecretStr

from montaj.budget import Budget
from montaj.candidates import (
    CandidateBounds,
    MomentType,
    Pick,
    find_candidates,
    snap_pick,
    unique_picks,
    windows,
)
from montaj.config import Settings
from montaj.costs import cost_totals
from montaj.db import connect, fetch
from montaj.external import ExternalCallError
from montaj.models import Clip, ClipStatus
from montaj.transcribe import Transcript, Word
from tests.fakes import FakeLLM


def test_truncated_candidate_completion_does_not_repeat_the_paid_request(
    prepared_source: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = prepared_source.data_dir / "sources/1"
    transcript = Transcript(
        text="A fixture",
        language="en",
        words=[Word(text="word", start=t, end=t + 0.5) for t in range(35)],
    )
    (directory / "transcript.json").write_text(
        transcript.model_dump_json(), encoding="utf-8"
    )
    settings = prepared_source.model_copy(
        update={"anthropic_api_key": SecretStr("offline-fake")}
    )
    requests = []

    def send(request: Request, *, timeout: int) -> BytesIO:
        requests.append(request.data)
        return BytesIO(
            json.dumps(
                {
                    "content": [{"type": "text", "text": '[{"start":'}],
                    "stop_reason": "max_tokens",
                    "usage": {"input_tokens": 1000, "output_tokens": 2200},
                }
            ).encode()
        )

    monkeypatch.setattr("montaj.claude.urlopen", send)
    with pytest.raises((ExternalCallError, ValueError)) as error:
        find_candidates(settings, 1, bounds=CandidateBounds(), budget=Budget(1, 1))

    with connect(settings.db_path) as db:
        totals = cost_totals(db, 1)
    assert (len(requests), totals.total) == (1, pytest.approx(0.024))
    assert isinstance(error.value, ExternalCallError)
    assert "truncated at 2200 tokens" in str(error.value)
    assert not (directory / "candidates.json").exists()
    assert not list((directory / ".candidate-windows").rglob("*.json"))
    assert totals.by_outcome["failure"].count == 1


def _pick(start: float, end: float) -> Pick:
    return Pick(
        start=start,
        end=end,
        moment_type=MomentType.STORY,
        reason="Stands alone",
        hook_idea="A hook",
    )


def test_windows_overlap_by_one_minute() -> None:
    words = [Word(text="word", start=t, end=t + 0.5) for t in range(840)]
    first, second = windows(words)
    assert first[0].start == 0 and first[-1].start == 449
    assert second[0].start == 390 and second[-1].start == 839
    assert len({word.start for word in first} & {word.start for word in second}) == 60


def test_snap_prefers_sentence_end_and_deduplicates() -> None:
    words = [
        Word(text="Done." if t == 24 else "word", start=t, end=t + 0.5)
        for t in range(40)
    ]
    snapped = snap_pick(_pick(1.1, 23.1), words, CandidateBounds())
    assert snapped is not None and (snapped.start, snapped.end) == (1, 24.5)
    assert unique_picks([snapped, _pick(3, 24), _pick(27, 39)]) == [
        snapped,
        _pick(27, 39),
    ]
    assert snap_pick(_pick(30, 40), words, CandidateBounds()) is None


def test_finds_caches_and_persists_candidates(prepared_source: Settings) -> None:
    directory = prepared_source.data_dir / "sources/1"
    words = [
        Word(text="Fin." if i == 12 else "mot", start=i, end=i + 0.5) for i in range(18)
    ]
    transcript = Transcript(
        text=" ".join(word.text for word in words), language="fr", words=words
    )
    (directory / "transcript.json").write_text(
        transcript.model_dump_json(), encoding="utf-8"
    )
    fake = FakeLLM(["bad JSON", json.dumps([_pick(3.1, 12).model_dump()])])
    settings = prepared_source
    bounds = CandidateBounds(min_seconds=5, max_seconds=15)

    result, reused = find_candidates(
        settings, 1, bounds=bounds, budget=Budget(1, 1), llm=fake
    )
    assert not reused and len(result.candidates) == 1
    assert '"language":"fr"' in fake.prompts[0]
    clip_id = result.candidates[0].clip_id
    with connect(settings.db_path) as db:
        clip = fetch(db, Clip, clip_id)
        assert clip is not None
        assert clip.status == ClipStatus.CANDIDATE
        assert (clip.start_s, clip.end_s) == (3, 12.5)

    assert find_candidates(
        settings, 1, bounds=bounds, budget=Budget(1, 1), llm=fake
    ) == (result, True)
    assert len(fake.prompts) == 2
