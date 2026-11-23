"""Deterministic clean-up of model-picked moments: whole sentences, no repeats.

Claude proposes rough moments. These rules move each start and end to a nearby
sentence or pause boundary when one is close enough, then drop picks that overlap
or repeat what an earlier pick already says. Weak signals leave the pick unchanged.
"""

import re
from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from montaj.transcribe import Word

SENTENCE_END = (".", "?", "!", "؟", "。", "！", "？", "…")
CLOSERS = "\"'”’)]}»"
PAUSE_SECONDS = 0.6
START_BACK_SECONDS = 6.0
START_FORWARD_SECONDS = 4.0
END_WINDOW_SECONDS = 8.0
PAUSE_END_WINDOW = 3.0
DANGLING_TAIL = 1.5
OVERLAP_SECONDS = 3.0
OVERLAP_SHARE = 0.15
NEAR_DUPLICATE = 0.55
SHINGLE = 3
MIN_SHINGLES = 5
DANGLING = frozenset(
    {
        "and", "but", "so", "or", "because", "that", "which", "who", "the", "a",
        "an", "to", "of", "with", "if", "when", "then", "like",
        "و", "لكن", "ثم", "لأن", "لان", "اللي", "الذي", "التي",
    }
)  # fmt: skip
_TOKEN = re.compile(r"\w+", re.UNICODE)


class Span(Protocol):
    @property
    def start(self) -> float: ...

    @property
    def end(self) -> float: ...


@dataclass(frozen=True, slots=True)
class Boundary:
    start: float
    end: float
    clean_start: bool
    clean_end: bool
    moved: bool = False


def ends_sentence(text: str) -> bool:
    return text.rstrip(CLOSERS).endswith(SENTENCE_END)


def _bare(text: str) -> str:
    return "".join(_TOKEN.findall(text.casefold()))


def is_start(words: Sequence[Word], index: int) -> bool:
    """A sentence or thought starts here: first word, after punctuation, or a pause."""
    if index == 0:
        return True
    previous = words[index - 1]
    return (
        ends_sentence(previous.text)
        or words[index].start - previous.end >= PAUSE_SECONDS
    )


def is_end(words: Sequence[Word], index: int) -> bool:
    return ends_sentence(words[index].text)


def _pause_after(words: Sequence[Word], index: int) -> bool:
    return (
        index + 1 >= len(words)
        or words[index + 1].start - words[index].end >= PAUSE_SECONDS
    )


def _find(words: Sequence[Word], starts: list[float], value: float) -> int:
    index = bisect_left(starts, value - 1e-6)
    return min(index, len(words) - 1)


def _best_start(words: Sequence[Word], starts: list[float], index: int) -> int:
    if is_start(words, index):
        return index
    origin = words[index].start
    low = bisect_left(starts, origin - START_BACK_SECONDS)
    high = bisect_right(starts, origin + START_FORWARD_SECONDS)
    # Moving back keeps the context a listener needs. Move forward only as a fallback.
    back = [i for i in range(low, index) if is_start(words, i)]
    if back:
        return back[-1]
    forward = [i for i in range(index + 1, high) if is_start(words, i)]
    return forward[0] if forward else index


def _best_end(
    words: Sequence[Word],
    first: int,
    target: float,
    min_seconds: float,
    max_seconds: float,
) -> int | None:
    start = words[first].start
    eligible = [
        i
        for i in range(first, len(words))
        if min_seconds - 1e-8 <= words[i].end - start <= max_seconds + 1e-8
    ]
    if not eligible:
        return None
    sentences = [
        i
        for i in eligible
        if is_end(words, i) and abs(words[i].end - target) <= END_WINDOW_SECONDS
    ]
    if sentences:
        return min(sentences, key=lambda i: (abs(words[i].end - target), -i))
    pauses = [
        i
        for i in eligible
        if _pause_after(words, i) and abs(words[i].end - target) <= PAUSE_END_WINDOW
    ]
    if pauses:
        chosen = min(pauses, key=lambda i: (abs(words[i].end - target), -i))
    else:
        chosen = min(eligible, key=lambda i: abs(words[i].end - target))
    # Never stop on a connector that promises more ("and", "because").
    while (
        _bare(words[chosen].text) in DANGLING
        and chosen - 1 in eligible
        and words[chosen].end - words[chosen - 1].end <= DANGLING_TAIL
    ):
        chosen -= 1
    return chosen


def refine(
    start: float,
    end: float,
    words: Sequence[Word],
    min_seconds: float,
    max_seconds: float,
) -> Boundary | None:
    """Move a word-aligned span to whole-sentence edges within the duration bounds."""
    if not words:
        return None
    starts = [word.start for word in words]
    first = _best_start(words, starts, _find(words, starts, start))
    last = _best_end(words, first, end, min_seconds, max_seconds)
    if last is None and first != _find(words, starts, start):
        first = _find(words, starts, start)
        last = _best_end(words, first, end, min_seconds, max_seconds)
    if last is None:
        return None
    return Boundary(
        words[first].start,
        words[last].end,
        is_start(words, first),
        is_end(words, last) or _pause_after(words, last),
        abs(words[first].start - start) > 1e-6 or abs(words[last].end - end) > 1e-6,
    )


def _overlaps(a: Span, b: Span) -> bool:
    shared = min(a.end, b.end) - max(a.start, b.start)
    shorter = min(a.end - a.start, b.end - b.start)
    return shared >= OVERLAP_SECONDS or shared >= OVERLAP_SHARE * shorter


def _shingles(words: Sequence[Word], span: Span) -> set[tuple[str, ...]]:
    tokens = [
        _bare(word.text)
        for word in words
        if span.start - 1e-6 <= word.start and word.end <= span.end + 1e-6
    ]
    tokens = [token for token in tokens if token]
    if len(tokens) < SHINGLE:
        return {tuple(tokens)} if tokens else set()
    return {tuple(tokens[i : i + SHINGLE]) for i in range(len(tokens) - SHINGLE + 1)}


def similarity(words: Sequence[Word], a: Span, b: Span) -> float:
    first, second = _shingles(words, a), _shingles(words, b)
    # Too little distinct text to call two picks the same thing.
    if min(len(first), len(second)) < MIN_SHINGLES:
        return 0.0
    return len(first & second) / min(len(first), len(second))


def is_duplicate(words: Sequence[Word], candidate: Span, kept: Sequence[Span]) -> bool:
    return any(
        _overlaps(candidate, other)
        or similarity(words, candidate, other) >= NEAR_DUPLICATE
        for other in kept
    )
