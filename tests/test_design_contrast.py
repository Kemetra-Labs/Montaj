"""WCAG AA contrast for every text colour on the backgrounds the board puts it on.

The tokens live in `dashboard.css` as `oklch()`. They are converted to sRGB
here (OKLab to linear sRGB with the standard matrices, then clamped), so a
token change that drops a pair below AA fails a test, not only an eye check.
"""

import math
import re
from pathlib import Path

import pytest

_CSS = Path(__file__).parent.parent / "src" / "montaj" / "dashboard.css"
_TOKEN = re.compile(r"--([a-z-]+):\s*oklch\(([\d.]+)%\s+([\d.]+)\s+([\d.]+)\)")

RGB = tuple[float, float, float]


def _tokens() -> dict[str, RGB]:
    root = _CSS.read_text(encoding="utf-8").split("}", 1)[0]
    return {
        name: _oklch_to_srgb(float(lightness) / 100, float(chroma), float(hue))
        for name, lightness, chroma, hue in _TOKEN.findall(root)
    }


def _oklch_to_srgb(lightness: float, chroma: float, hue: float) -> RGB:
    """Linear sRGB channels in 0..1, clamped to the gamut."""
    a = chroma * math.cos(math.radians(hue))
    b = chroma * math.sin(math.radians(hue))
    l_ = (lightness + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m_ = (lightness - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s_ = (lightness - 0.0894841775 * a - 1.2914855480 * b) ** 3
    red = 4.0767416621 * l_ - 3.3077115913 * m_ + 0.2309699292 * s_
    green = -1.2684380046 * l_ + 2.6097574011 * m_ - 0.3413193965 * s_
    blue = -0.0041960863 * l_ - 0.7034186147 * m_ + 1.7076147010 * s_
    return (
        min(max(red, 0.0), 1.0),
        min(max(green, 0.0), 1.0),
        min(max(blue, 0.0), 1.0),
    )


def _luminance(rgb: RGB) -> float:
    red, green, blue = rgb
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _contrast(foreground: RGB, background: RGB) -> float:
    high, low = sorted((_luminance(foreground), _luminance(background)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def test_conversion_is_right() -> None:
    white = _oklch_to_srgb(1.0, 0, 0)
    black = _oklch_to_srgb(0.0, 0, 0)
    assert all(abs(channel - 1.0) < 1e-3 for channel in white)
    assert black == (0.0, 0.0, 0.0)
    assert abs(_contrast(white, black) - 21) < 0.05


# Each text colour on each background the stylesheet puts it on.
_PAIRS = [
    *(
        (text, ground)
        for text in ("ink", "muted", "accent")
        for ground in ("canvas", "surface", "raised")
    ),
    ("warning", "warning-bg"),
    ("warning", "surface"),
    ("review", "review-bg"),
    ("review", "surface"),
    ("success", "success-bg"),
    ("success", "surface"),
    ("accent-ink", "accent"),
]


@pytest.mark.parametrize(("text", "ground"), _PAIRS)
def test_text_tokens_meet_aa(text: str, ground: str) -> None:
    tokens = _tokens()
    ratio = _contrast(tokens[text], tokens[ground])
    assert ratio >= 4.5, f"--{text} on --{ground} is {ratio:.2f}:1"
