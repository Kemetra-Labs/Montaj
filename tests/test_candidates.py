import json

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
from montaj.db import connect, fetch
from montaj.models import Clip, ClipStatus
from montaj.transcribe import Transcript, Word
from tests.fakes import FakeLLM


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
