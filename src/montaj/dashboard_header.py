"""The board home's header and summary strip. Markup only: figures come from `Summary`."""

from html import escape
from typing import TYPE_CHECKING

from montaj.dashboard_parts import BASE_PATH, format_usd

if TYPE_CHECKING:
    from montaj.board_view import Summary


def _money(amount: float) -> str:
    """Dollar text safe for HTML: `format_usd` can return `<$0.001`."""
    return escape(format_usd(amount))


def _stat(label: str, figure: str, note: str, tone: str = "neutral") -> str:
    return (
        f'<div class="stat" data-tone="{tone}"><dt>{label}</dt>'
        f'<dd class="figure">{figure}</dd><dd class="note">{note}</dd></div>'
    )


def _view_link(view: str, label: str) -> str:
    return f'<a href="{BASE_PATH}?view={view}">{label}</a>'


def _run_stat(summary: "Summary") -> str:
    if summary.run_spend is None:
        return _stat("Run ceiling", _money(summary.run_ceiling), "No job running")
    return _stat(
        "Run spend",
        _money(summary.run_spend),
        f"of {_money(summary.run_ceiling)} run ceiling",
    )


def summary_html(summary: "Summary") -> str:
    count = summary.source_count
    sources = f"{count} source" if count == 1 else f"{count} sources"
    flagged = summary.attention > 0
    waiting = summary.review_clips > 0
    return (
        '<dl class="summary panel" aria-label="Production summary">'
        + _stat("Est. API spend", _money(summary.spend), sources)
        + _run_stat(summary)
        + _stat(
            _view_link("attention", "Needs attention"),
            str(summary.attention),
            "Blocked or flagged" if flagged else "Nothing flagged",
            "warning" if flagged else "neutral",
        )
        + _stat(
            _view_link("review", "Clips awaiting review"),
            str(summary.review_clips),
            "Checked, from permitted sources" if waiting else "None checked yet",
            "review" if waiting else "neutral",
        )
        + "</dl>"
    )
