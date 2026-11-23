"""Score saved, excerpt-relative speech annotations without calling a provider."""

import argparse
import json
import re
from collections import Counter
from collections.abc import Sequence
from itertools import permutations
from pathlib import Path
from typing import Self

from pydantic import BaseModel, Field, TypeAdapter, model_validator

from montaj.transcribe import Transcript, Word
from scripts.pilot_paths import workspace_path

TOKEN = re.compile(r"[^\W_]+(?:'[^\W_]+)*")
BOUNDARY_TOLERANCE = 0.25


class Span(BaseModel):
    start: float = Field(ge=0, allow_inf_nan=False)
    end: float = Field(gt=0, allow_inf_nan=False)
    speaker: int | None = None

    @model_validator(mode="after")
    def validate_range(self) -> Self:
        if self.end <= self.start:
            raise ValueError("Span end must follow start.")
        return self


def _tokens(words: Sequence[Word]) -> list[tuple[str, Word]]:
    return [
        (token, word)
        for word in words
        for token in TOKEN.findall(word.text.casefold().replace("’", "'"))
    ]


def score_words(reference: Sequence[Word], hypothesis: Sequence[Word]) -> dict:
    """Compute edit distance, then timing error for exact lexical matches."""
    ref, hyp = _tokens(reference), _tokens(hypothesis)
    previous = [(index, 0, 0.0, 0.0) for index in range(len(hyp) + 1)]
    for index, (word, expected) in enumerate(ref, 1):
        row = [(index, 0, 0.0, 0.0)]
        for column, (token, observed) in enumerate(hyp, 1):
            edits, matches, start, end = previous[column - 1]
            if word == token:
                diagonal = (
                    edits,
                    matches - 1,
                    start + abs(expected.start - observed.start),
                    end + abs(expected.end - observed.end),
                )
            else:
                diagonal = (edits + 1, matches, start, end)
            removed, inserted = previous[column], row[-1]
            options = [
                diagonal,
                (removed[0] + 1, *removed[1:]),
                (inserted[0] + 1, *inserted[1:]),
            ]
            row.append(
                min(
                    options,
                    key=lambda result: (result[0], result[1], result[2] + result[3]),
                )
            )
        previous = row
    errors, negative_matches, start, end = previous[-1]
    matched = -negative_matches
    return {
        "reference_words": len(ref),
        "word_errors": errors,
        "wer": errors / len(ref) if ref else None,
        "matched_words": matched,
        "start_mae_seconds": start / matched if matched else None,
        "end_mae_seconds": end / matched if matched else None,
    }


def _boundaries(spans: Sequence[Span]) -> tuple[list[float], list[float]]:
    starts: list[float] = []
    ends: list[float] = []
    for span in sorted(spans, key=lambda span: span.start):
        if not ends or span.start > ends[-1]:
            starts.append(span.start)
            ends.append(span.end)
        else:
            ends[-1] = max(ends[-1], span.end)
    return starts, ends


def _boundary_errors(
    reference: Sequence[Span], hypothesis: Sequence[Span]
) -> tuple[list[float], int, int]:
    errors: list[float] = []
    missed = extra = 0
    for expected, observed in zip(
        _boundaries(reference), _boundaries(hypothesis), strict=True
    ):
        first = second = 0
        while first < len(expected) and second < len(observed):
            difference = observed[second] - expected[first]
            if abs(difference) <= BOUNDARY_TOLERANCE:
                errors.append(abs(difference))
                first += 1
                second += 1
            elif difference < 0:
                second += 1
                extra += 1
            else:
                first += 1
                missed += 1
        missed += len(expected) - first
        extra += len(observed) - second
    return errors, missed, extra


def _frames(reference: Sequence[Span], hypothesis: Sequence[Span]):
    events = sorted(
        (
            (time, index, span.speaker, change)
            for index, spans in enumerate((reference, hypothesis))
            for span in spans
            for time, change in ((span.start, 1), (span.end, -1))
        ),
        key=lambda event: event[0],
    )
    active: tuple[Counter, Counter] = (Counter(), Counter())
    previous = 0.0
    for time, index, speaker, change in events:
        if time > previous:
            yield time - previous, set(active[0]), set(active[1])
        active[index][speaker] += change
        if active[index][speaker] == 0:
            del active[index][speaker]
        previous = time


def score_activity(
    reference: Sequence[Span], hypothesis: Sequence[Span], *, diarization: bool = False
) -> dict:
    frames = list(_frames(reference, hypothesis))
    errors, missed, extra = _boundary_errors(reference, hypothesis)
    result = {
        "reference_speech_seconds": sum(seconds for seconds, ref, _ in frames if ref),
        "missed_speech_seconds": sum(
            seconds for seconds, ref, hyp in frames if ref and not hyp
        ),
        "false_speech_seconds": sum(
            seconds for seconds, ref, hyp in frames if hyp and not ref
        ),
        "matched_boundaries": len(errors),
        "boundary_mae_seconds": sum(errors) / len(errors) if errors else None,
        "unmatched_reference_boundaries": missed,
        "unmatched_hypothesis_boundaries": extra,
    }
    if not diarization:
        return result

    labels = sorted({span.speaker for span in reference if span.speaker is not None})
    hyp_labels = sorted(
        {span.speaker for span in hypothesis if span.speaker is not None}
    )
    if len(labels) != 2 or any(
        span.speaker is None for span in (*reference, *hypothesis)
    ):
        raise ValueError(
            "Diarization requires two labeled reference speakers and labels on all spans."
        )
    overlaps: Counter = Counter()
    for seconds, ref, hyp in frames:
        for expected in ref:
            for observed in hyp:
                overlaps[expected, observed] += seconds
    # None represents an unmatched reference speaker.
    choices = [*hyp_labels, None]
    first, second = max(
        [(None, None), *permutations(choices, 2)],
        key=lambda pair: overlaps[labels[0], pair[0]] + overlaps[labels[1], pair[1]],
    )
    mapping = dict(zip(labels, (first, second), strict=True))
    correct = sum(overlaps[label, observed] for label, observed in mapping.items())
    total = sum(seconds * len(ref) for seconds, ref, _ in frames)
    wrong = (
        sum(seconds * max(len(ref), len(hyp)) for seconds, ref, hyp in frames) - correct
    )
    turn_errors: list[float] = []
    unmatched_ref = unmatched_hyp = 0
    for label, observed in mapping.items():
        turn, missing, spurious = _boundary_errors(
            [span for span in reference if span.speaker == label],
            [span for span in hypothesis if span.speaker == observed],
        )
        turn_errors.extend(turn)
        unmatched_ref += missing
        unmatched_hyp += spurious
    assigned = set(mapping.values())
    unmatched_hyp += sum(
        sum(
            map(
                len, _boundaries([span for span in hypothesis if span.speaker == label])
            )
        )
        for label in hyp_labels
        if label not in assigned
    )
    return result | {
        "reference_speaker_seconds": total,
        "diarization_error_rate": max(wrong, 0) / total,
        "matched_turn_boundaries": len(turn_errors),
        "turn_boundary_mae_seconds": sum(turn_errors) / len(turn_errors)
        if turn_errors
        else None,
        "unmatched_reference_turn_boundaries": unmatched_ref,
        "unmatched_hypothesis_turn_boundaries": unmatched_hyp,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("words", "vad", "diarization"))
    parser.add_argument("reference", type=Path)
    parser.add_argument("hypothesis", type=Path)
    args = parser.parse_args()
    try:
        reference_path = workspace_path(args.reference)
        hypothesis_path = workspace_path(args.hypothesis)
        if not reference_path.is_file() or not hypothesis_path.is_file():
            raise ValueError("Reference and hypothesis must be existing JSON files.")
        reference, hypothesis = (
            reference_path.read_bytes(),
            hypothesis_path.read_bytes(),
        )
        if args.mode == "words":
            scores = score_words(
                Transcript.model_validate_json(reference).words,
                Transcript.model_validate_json(hypothesis).words,
            )
        else:
            spans = TypeAdapter(list[Span])
            scores = score_activity(
                spans.validate_json(reference),
                spans.validate_json(hypothesis),
                diarization=args.mode == "diarization",
            )
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Speech probe failed: {exc}\n")
    print(json.dumps(scores, indent=2))


if __name__ == "__main__":
    main()
