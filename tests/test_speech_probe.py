import pytest

from montaj.transcribe import Word
from scripts.speech_probe import Span, score_activity, score_words


def test_word_error_rate_and_timing_use_aligned_equal_words() -> None:
    reference = [Word(text="One,", start=0, end=1), Word(text="two", start=1, end=2)]
    candidate = [
        Word(text="ONE", start=0.2, end=0.8),
        Word(text="three", start=1, end=2),
    ]
    assert score_words(reference, candidate) == {
        "reference_words": 2,
        "word_errors": 1,
        "wer": 0.5,
        "matched_words": 1,
        "start_mae_seconds": pytest.approx(0.2),
        "end_mae_seconds": pytest.approx(0.2),
    }
    assert score_words(reference, [])["start_mae_seconds"] is None
    assert score_words([], candidate)["wer"] is None


def test_activity_distinguishes_false_speech_misses_and_boundary_errors() -> None:
    scores = score_activity([Span(start=1, end=3)], [Span(start=0.875, end=2.75)])
    assert scores["false_speech_seconds"] == 0.125
    assert scores["missed_speech_seconds"] == 0.25
    assert scores["boundary_mae_seconds"] == 0.1875
    assert scores["matched_boundaries"] == 2
    assert scores["unmatched_reference_boundaries"] == 0


def test_two_speaker_diarization_maps_labels_and_counts_overlap() -> None:
    ref = [Span(start=0, end=2, speaker=1), Span(start=1, end=3, speaker=2)]
    hyp = [Span(start=0, end=2, speaker=9), Span(start=2, end=3, speaker=7)]
    scores = score_activity(ref, hyp, diarization=True)
    assert scores["missed_speech_seconds"] == 0
    assert scores["diarization_error_rate"] == 0.25
    assert scores["reference_speaker_seconds"] == 4


def test_spans_reject_reversed_and_unlabeled_diarization() -> None:
    with pytest.raises(ValueError, match="end"):
        Span(start=2, end=1)
    with pytest.raises(ValueError, match="labeled"):
        score_activity(
            [Span(start=0, end=1, speaker=1), Span(start=1, end=2, speaker=2)],
            [Span(start=0, end=1)],
            diarization=True,
        )
