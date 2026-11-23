"""Markup primitives and shared state for the dashboard pages."""

from dataclasses import dataclass
from html import escape

BASE_PATH = "/dashboard"
STAGE_PATH = f"{BASE_PATH}/stage"
JOBS_LINK = f"{BASE_PATH}/jobs"
NEW_JOB_LINK = "#new-job"
_REFRESH_SECONDS = 3
STATE_LABELS = {
    "done": "Done",
    "running": "Running",
    "blocked": "Blocked",
    "failed": "Failed",
    "pending": "Pending",
}
NOT_PERMITTED_REASON = "Record permission for this source first."
NO_BASIS_REASON = "Record a permission basis for this source first."
PERMISSION_REASONS = frozenset({NOT_PERMITTED_REASON, NO_BASIS_REASON})


def permission_reason(allowed: bool, basis: str | None) -> str:
    """Why a source may not run, or an empty string when it may.

    The server refuses a source with no recorded basis, so the board does too.
    """
    if not allowed:
        return NOT_PERMITTED_REASON
    if not (basis or "").strip():
        return NO_BASIS_REASON
    return ""


def render_dashboard_page(
    title: str, content: str, active: str = "", refresh: bool = False
) -> bytes:
    reload = (
        f'<meta http-equiv="refresh" content="{_REFRESH_SECONDS}">' if refresh else ""
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">{reload}
<title>{escape(title)} · Montaj</title><link rel="stylesheet" href="/dashboard/style.css"></head>
<body><a class="skip" href="#main">Skip to content</a><div class="shell"><header class="rail">
<a class="brand" href="/dashboard"><span class="brand-mark" aria-hidden="true">M</span>Montaj</a>
<nav aria-label="Main"><a href="/dashboard" {active}>Production board</a><a href="/">Clip preview <span aria-hidden="true">↗</span></a></nav>
</header><main class="frame" id="main">{content}</main></div></body></html>""".encode()


def badge(text: str, tone: str) -> str:
    return f'<span class="badge {tone}">{escape(text)}</span>'


@dataclass(frozen=True, slots=True)
class LiveJob:
    """A snapshot of the dashboard's current or latest background job."""

    source_id: int | None
    stage: str
    running: bool
    elapsed_s: float
    spend: float | None
    summary: str
    problem: str


@dataclass(frozen=True, slots=True)
class BoardContext:
    """What the board needs besides the rows: live job state and write access."""

    operations: bool = False
    running: tuple[int, str] | None = None
    token: str = ""
    notice: str = ""
    run_spend: tuple[float, float] | None = None
    live: LiveJob | None = None
    campaign: str = ""
    source_ceiling: float = 0.0
    campaign_choices: tuple[tuple[int, str], ...] = ()


def hidden(**fields: str | int) -> str:
    return "".join(
        f'<input type="hidden" name="{name}" value="{escape(str(value), quote=True)}">'
        for name, value in fields.items()
    )


def link_button(href: str, label: str) -> str:
    """A primary link. The caller passes an href that is already HTML-safe."""
    return f'<a class="button primary" href="{href}">{escape(label)}</a>'


def busy_html(label: str, running: tuple[int, str], describe: str = "") -> str:
    """A disabled button and why. `describe` links the two for screen readers."""
    reason = "Another job is running"
    if running[0]:
        reason += f" (source {running[0]})"
    by = f' aria-describedby="{describe}"' if describe else ""
    anchor = f' id="{describe}"' if describe else ""
    return (
        f'<button disabled class="button"{by}>{escape(label)}</button>'
        f'<span class="secondary"{anchor}>{reason}</span>'
    )


def parse_source_id(value: str) -> int | None:
    if len(value) <= 19 and value.isascii() and value.isdecimal():
        number = int(value)
        if 0 < number <= 2**63 - 1:
            return number
    return None
