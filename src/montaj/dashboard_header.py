"""The board home's header and summary strip. Markup only: figures come from `Summary`."""

from html import escape
from typing import TYPE_CHECKING

from montaj.dashboard_parts import BASE_PATH, format_usd, render_board_page

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


def header_html(live: bool, home: bool = True) -> str:
    """Brand, page title and, while a job runs, a Live indicator that says so in words.

    The board home's title is its `<h1>`. Other pages link home by the same name
    and carry their own `<h1>` in the content.
    """
    title = (
        "<h1>Pipeline Control</h1>"
        if home
        else f'<a class="page-name" href="{BASE_PATH}">Pipeline Control</a>'
    )
    indicator = (
        '<p class="live"><span class="live-dot" aria-hidden="true"></span>Live</p>'
        if live
        else ""
    )
    return (
        '<header class="page-head">'
        f'<a class="brand" href="{BASE_PATH}"><span class="brand-mark" aria-hidden="true">M</span>Montaj</a>'
        '<span class="divider" aria-hidden="true"></span>'
        f"{title}{indicator}</header>"
    )


def render_page(
    title: str, content: str, *, live: bool = False, refresh: bool = False
) -> bytes:
    """A dashboard page other than the home: the board shell and header."""
    return render_board_page(title, header_html(live, home=False) + content, refresh)
