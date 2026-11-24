import math

import pytest

from montaj.dashboard_parts import format_usd


@pytest.mark.parametrize(
    ("amount", "text"),
    [
        (0, "$0.00"),
        (0.0001, "<$0.001"),
        (0.00049, "<$0.001"),
        (0.0005, "$0.001"),
        (0.004, "$0.004"),
        (0.01, "$0.01"),
        (0.012345, "$0.01"),
        (0.5, "$0.50"),
        (12.4666, "$12.47"),
    ],
)
def test_format_usd(amount: float, text: str) -> None:
    assert format_usd(amount) == text


@pytest.mark.parametrize("amount", [-0.01, math.nan, math.inf])
def test_format_usd_rejects_negative_and_non_finite(amount: float) -> None:
    with pytest.raises(ValueError, match="amount"):
        format_usd(amount)
