"""The controls bar: New job, the run ceiling meter, disk free and Refresh.

The meter is read only: nothing here removes or raises the spend ceiling.
"""

from html import escape
from typing import TYPE_CHECKING

from montaj.dashboard_parts import BASE_PATH, format_usd

if TYPE_CHECKING:
    from montaj.board_view import Summary

_GB = 1024**3
_MB = 1024**2


def _money(amount: float) -> str:
    """Dollar text safe for HTML: `format_usd` can return `<$0.001`."""
    return escape(format_usd(amount))


def _meter_values(spent: float, ceiling: float) -> tuple[float, float]:
    """`max` and `value` for a native <meter>: value within 0..max, max above 0.

    A native meter needs no inline style, which the server's CSP blocks.
    """
    if ceiling > 0:
        return ceiling, min(max(spent, 0.0), ceiling)
    return 1.0, 0.0 if spent <= 0 else 1.0


def meter_html(summary: "Summary") -> str:
    ceiling = summary.run_ceiling
    if summary.run_spend is None:
        return (
            '<div class="control meter-block"><span class="control-label">Run ceiling</span>'
            f'<strong>{_money(ceiling)}</strong><span class="secondary">No job running</span></div>'
        )
    spent = summary.run_spend
    left = max(ceiling - spent, 0.0)
    top, value = _meter_values(spent, ceiling)
    return (
        '<div class="control meter-block"><span class="control-label">Run spend</span>'
        f'<meter class="meter" min="0" max="{top:g}" value="{value:g}" '
        'aria-label="Run spend against the run ceiling"></meter>'
        f'<span class="secondary">{_money(spent)} of {_money(ceiling)} · '
        f"{_money(left)} left</span></div>"
    )


def disk_html(free_bytes: int | None, min_free_mb: float) -> str:
    """Free space on the data volume, with the level `montaj doctor` warns at."""
    if free_bytes is None:
        return (
            '<div class="control disk"><span class="control-label">Data volume</span>'
            "<strong>Disk space unavailable</strong></div>"
        )
    low = free_bytes / _MB < min_free_mb
    warning = (
        f'<span class="secondary">Below the {min_free_mb:.0f} MB warning level</span>'
        if low
        else ""
    )
    tone = "disk warning" if low else "disk"
    return (
        f'<div class="control {tone}"><span class="control-label">Data volume</span>'
        f"<strong>{free_bytes / _GB:.1f} GB free</strong>{warning}</div>"
    )


def controls_html(
    summary: "Summary",
    new_job: str,
    view: str,
    free_bytes: int | None,
    min_free_mb: float,
) -> str:
    """The bar. `new_job` is the form's markup, empty on a read-only board."""
    refresh = escape(f"{BASE_PATH}?view={view}", quote=True)
    return (
        '<section class="controls panel" aria-label="Controls">'
        f"{new_job}{meter_html(summary)}{disk_html(free_bytes, min_free_mb)}"
        f'<a class="refresh" href="{refresh}">Refresh</a></section>'
    )
