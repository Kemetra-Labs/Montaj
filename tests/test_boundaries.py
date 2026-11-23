"""MJ-5 fixtures: whole-sentence edges, overlap and near-repeat suppression."""

import json

import pytest

from montaj import candidates as candidates_module
from montaj.boundaries import is_duplicate, refine, similarity
from montaj.budget import Budget
from montaj.candidates import (
    CandidateBounds,
    MomentType,
    Pick,
    campaign_bounds,
    clean_picks,
    find_candidates,
)
from montaj.config import Settings
from montaj.models import Campaign, CampaignRules, Platform
from montaj.transcribe import Transcript, Word
from tests.fakes import FakeLLM


def speech(text: str, start: float = 0.0, step: float = 0.5) -> list[Word]:
    """Evenly timed words. A "|" marks a 1 s pause before the next word."""
    words, t = [], start
    for token in text.split():
        if token == "|":
            t += 1.0
            continue
        words.append(Word(text=token, start=t, end=t + step * 0.8))
        t += step
    return words


def pick(start: float, end: float) -> Pick:
    return Pick(
        start=start, end=end, moment_type=MomentType.STORY, reason="r", hook_idea="h"
    )


SENTENCES = (
    "We started small. The first year was hard because nobody knew us at all. "
    "And then one customer changed everything for the team. "
    "She told her friends and they told theirs. That is how it grew."
)


def test_mid_sentence_start_moves_back_to_the_sentence_start() -> None:
    words = speech(SENTENCES)
    # "nobody" is mid-sentence. "The" begins that sentence 3.5 s earlier.
    start = next(w.start for w in words if w.text == "nobody")
    edge = refine(start, words[-1].end, words, 5, 60)
    assert edge is not None and edge.clean_start
    assert edge.start == next(w.start for w in words if w.text == "The")


def test_and_after_a_full_stop_is_a_valid_standalone_start() -> None:
    words = speech(SENTENCES)
    start = next(w.start for w in words if w.text == "And")
    edge = refine(start, words[-1].end, words, 5, 60)
    assert edge is not None and edge.start == start


def test_end_prefers_a_quoted_sentence_end_nearby() -> None:
    words = speech('He said "we will ship it." Then he left the room quietly')
    edge = refine(0.0, words[-1].end, words, 1, 60)
    assert edge is not None and edge.clean_end
    assert words[[w.end for w in words].index(edge.end)].text == 'it."'


def test_end_falls_back_to_a_pause_then_trims_a_dangling_connector() -> None:
    paused = speech("one two three four | " + "word " * 20)
    edge = refine(0.0, 2.5, paused, 1, 60)
    assert edge is not None and edge.end == paused[3].end
    dangling = speech("we kept going every day and")
    edge = refine(0.0, dangling[-1].end, dangling, 1, 60)
    assert edge is not None and edge.end == dangling[-2].end


def test_boundary_moves_respect_the_duration_bounds() -> None:
    words = speech(SENTENCES)
    start = next(w.start for w in words if w.text == "nobody")
    # Moving back to "The" still allows a whole sentence inside 5-10 s.
    edge = refine(start, start + 9.5, words, 5, 10)
    assert edge is not None and edge.clean_start and edge.clean_end
    assert 5 <= edge.end - edge.start <= 10
    assert refine(0, 3, words, 40, 60) is None


def test_overlap_of_a_quarter_is_now_suppressed() -> None:
    # The #46/#47 pattern: 6.479 s shared, 25.67% of the shorter clip.
    earlier, later = pick(0.0, 48.08), pick(48.08 - 6.479, 48.08 - 6.479 + 25.24)
    assert is_duplicate([], later, [earlier])
    assert not is_duplicate([], pick(49.0, 60.0), [earlier])


def test_near_identical_and_contained_pairs_are_suppressed() -> None:
    # #33/#48: the same excerpt 81 ms apart. #18 contains all of #56.
    assert is_duplicate([], pick(10.081, 35.9), [pick(10.0, 35.82)])
    assert is_duplicate([], pick(20.0, 45.62), [pick(5.0, 59.9)])


def test_repeated_text_at_different_times_is_a_near_duplicate() -> None:
    story = "the best advice I ever got was to call customers myself every week"
    words = speech(story) + speech(story, start=100.0)
    first, again = pick(0.0, 6.5), pick(100.0, 106.5)
    assert similarity(words, first, again) == pytest.approx(1.0)
    assert is_duplicate(words, again, [first])


def test_clean_picks_keeps_the_complete_pick_of_an_overlapping_pair() -> None:
    words = speech(SENTENCES)
    nobody = next(w.start for w in words if w.text == "nobody")
    whole = next(w.end for w in words if w.text == "all.")
    ragged, complete = pick(nobody, nobody + 6), pick(0.0, whole)
    kept = clean_picks([ragged, complete], words, CandidateBounds(min_seconds=3))
    assert len(kept) == 1 and kept[0].start == 0.0


def test_campaign_duration_rules_narrow_the_kept_range() -> None:
    campaign = Campaign(
        id=4,
        name="c",
        platform=Platform.YOUTUBE,
        rules_json=CampaignRules(min_seconds=30, max_seconds=45).model_dump_json(),
    )
    narrowed = campaign_bounds(CandidateBounds(), campaign)
    assert (narrowed.min_seconds, narrowed.max_seconds) == (30, 45)
    with pytest.raises(ValueError, match="fall outside"):
        campaign_bounds(CandidateBounds(min_seconds=5, max_seconds=10), campaign)


def test_clean_up_changes_reuse_cached_model_windows(
    prepared_source: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    words = speech(SENTENCES)
    transcript = Transcript(text=SENTENCES, language="en", words=words)
    (prepared_source.data_dir / "sources/1/transcript.json").write_text(
        transcript.model_dump_json(), encoding="utf-8"
    )
    fake = FakeLLM([json.dumps([pick(1.0, 9.0).model_dump()])])
    bounds = CandidateBounds(min_seconds=3, max_seconds=30)
    first, reused = find_candidates(
        prepared_source, 1, bounds=bounds, budget=Budget(1, 1), llm=fake
    )
    assert not reused and len(fake.prompts) == 1
    monkeypatch.setattr(candidates_module, "POSTPROCESS_VERSION", "candidates-post-v3")
    second, reused = find_candidates(
        prepared_source, 1, bounds=bounds, budget=Budget(1, 1), llm=fake
    )
    assert not reused and second.input_hash != first.input_hash
    assert len(fake.prompts) == 1  # no new paid request
