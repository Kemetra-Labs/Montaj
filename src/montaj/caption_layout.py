"""Group timed words into readable two-line captions with word-level highlights.

The ASS engine stays. This module only decides which words share a screen, where the
lines break and how long each highlight lasts. Widths are estimated from the font
size, and a libass render test checks the estimate against real extents.
"""

import unicodedata
from dataclasses import dataclass

SAFE_WIDTH = 880  # 1080 wide video minus 100 px side margins
WIDTH_FILL = 0.94
SENTENCE_END = (".", "?", "!", "؟", "。", "！", "？", "…")
LONG_PAUSE = 0.7  # seconds of silence that end a caption group
LINGER = 0.3  # a finished group stays this long if nothing follows
MAX_LINES = 2


@dataclass(frozen=True, slots=True)
class TimedWord:
    start: float
    end: float
    text: str


@dataclass(frozen=True, slots=True)
class CaptionEvent:
    start: float
    end: float
    lines: tuple[tuple[str, ...], ...]
    active: int  # index of the highlighted word within the flattened group


def _char_em(char: str) -> float:
    if char.isspace():
        return 0.27
    category = unicodedata.category(char)
    if category.startswith("P") or category.startswith("S"):
        return 0.34
    if category == "Nd":
        return 0.6
    if "؀" <= char <= "ۿ" or "ݐ" <= char <= "ݿ":
        return 0.52
    if unicodedata.east_asian_width(char) in {"W", "F"}:
        return 1.0
    return 0.7 if char.isupper() else 0.56


def text_width(text: str, size: int, outline: float = 0) -> float:
    """Estimated rendered width in pixels, with room for the outline."""
    return sum(_char_em(char) for char in text) * size + 2 * outline


def _ends_sentence(text: str) -> bool:
    return text.rstrip("\"'”’)]}").endswith(SENTENCE_END)


def build_groups(
    words: list[TimedWord], size: int, outline: float, words_per_line: int
) -> list[list[list[int]]]:
    """Split word indexes into groups of at most two lines that fit the safe width."""
    budget = SAFE_WIDTH * WIDTH_FILL
    groups: list[list[list[int]]] = []
    lines: list[list[int]] = [[]]
    used = 0.0
    for index, word in enumerate(words):
        width = text_width(word.text, size, 0)
        space = text_width(" ", size) if lines[-1] else 0.0
        full = bool(lines[-1]) and (
            len(lines[-1]) >= words_per_line
            or used + space + width + 2 * outline > budget
        )
        if full:
            if len(lines) == MAX_LINES:
                groups.append([line for line in lines if line])
                lines = [[]]
            else:
                lines.append([])
            used, space = 0.0, 0.0
        lines[-1].append(index)
        used += space + width
        gap = words[index + 1].start - word.end if index + 1 < len(words) else 0.0
        if sum(len(line) for line in lines) >= 2 and (
            _ends_sentence(word.text) or gap > LONG_PAUSE
        ):
            groups.append([line for line in lines if line])
            lines, used = [[]], 0.0
    if any(lines):
        groups.append([line for line in lines if line])
    return groups


def caption_events(
    words: list[TimedWord],
    duration: float,
    *,
    size: int,
    outline: float,
    words_per_line: int,
    max_gap: float = 0.5,
) -> list[CaptionEvent]:
    """One event per word, so highlights move while the group text stays on screen."""
    groups = build_groups(words, size, outline, words_per_line)
    events: list[CaptionEvent] = []
    for number, group in enumerate(groups):
        flat = [index for line in group for index in line]
        following = (
            words[groups[number + 1][0][0]].start
            if number + 1 < len(groups)
            else duration
        )
        text = tuple(tuple(words[index].text for index in line) for line in group)
        for position, index in enumerate(flat):
            word = words[index]
            if position + 1 < len(flat):
                end = min(words[flat[position + 1]].start, word.end + max_gap)
            else:
                end = min(word.end + LINGER, following)
            end = min(end, duration)
            if end - word.start > 0:
                events.append(CaptionEvent(word.start, end, text, position))
    return events
