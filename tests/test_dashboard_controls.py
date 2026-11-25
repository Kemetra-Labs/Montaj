import pytest

from montaj.board_view import Summary
from montaj.dashboard_controls import controls_html, disk_html, meter_html

_GB = 1024**3
_FORM = '<details id="new-job" class="panel job-panel"><summary>New job</summary><form></form></details>'


def _summary(spent: float | None = None, ceiling: float = 0.5) -> Summary:
    return Summary(
        spend=0.0,
        source_count=0,
        run_spend=spent,
        run_ceiling=ceiling,
        attention=0,
        review_clips=0,
    )


def _text(html: str) -> str:
    import re

    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


def test_idle_meter_shows_the_ceiling_only() -> None:
    html = meter_html(_summary())
    assert "Run ceiling $0.50" in _text(html) and "No job running" in html
    assert "<meter" not in html


def test_running_meter() -> None:
    html = meter_html(_summary(0.12))
    assert "Run spend $0.12 of $0.50 · $0.38 left" in _text(html)
    assert '<meter class="meter" min="0" max="0.5" value="0.12"' in html
    assert "style=" not in html  # the server's CSP blocks inline styles


@pytest.mark.parametrize(
    ("spent", "ceiling", "bounds", "left"),
    [
        (0.7, 0.5, 'max="0.5" value="0.5"', "$0.00 left"),
        (0.0, 0.0, 'max="1" value="0"', "$0.00 left"),
        (0.1, 0.0, 'max="1" value="1"', "$0.00 left"),
    ],
)
def test_meter_bounds(spent: float, ceiling: float, bounds: str, left: str) -> None:
    html = meter_html(_summary(spent, ceiling))
    assert bounds in html and left in html


def test_meter_escapes_a_tiny_spend() -> None:
    html = meter_html(_summary(0.0002))
    assert "&lt;$0.001" in html and "<$0.001" not in html


def test_disk_free() -> None:
    assert "12.3 GB free" in disk_html(int(12.3 * _GB), 1024)
    assert "Disk space unavailable" in disk_html(None, 1024)
    low = disk_html(512 * 1024**2, 1024)
    assert 'class="control disk disk-low"' in low
    assert "Below the 1024 MB warning level" in low
    assert "warning" not in disk_html(int(12.3 * _GB), 1024)


def test_read_only_controls_have_no_new_job() -> None:
    html = controls_html(_summary(), "", "all", 5 * _GB, 1024)
    assert "new-job" not in html and "<form" not in html
    assert "Run ceiling" in html and "GB free" in html and ">Refresh</a>" in html


def test_controls_hold_the_new_job_form() -> None:
    html = controls_html(_summary(), _FORM, "all", 5 * _GB, 1024)
    assert _FORM in html and 'class="controls panel"' in html
    assert html.index(_FORM) < html.index("Run ceiling")


def test_refresh_keeps_the_view() -> None:
    html = controls_html(_summary(), "", "attention", 5 * _GB, 1024)
    assert '<a class="refresh" href="/dashboard?view=attention">Refresh</a>' in html


def test_no_ceiling_control() -> None:
    html = controls_html(_summary(0.1), _FORM, "all", 5 * _GB, 1024).replace(_FORM, "")
    assert "<input" not in html and "<button" not in html and "<form" not in html


def test_low_disk_is_shown_in_megabytes() -> None:
    low = disk_html(40 * 1024**2, 1024)
    assert "40 MB free" in low and "GB free" not in low
    assert "warning" not in disk_html(1024 * 1024**2, 1024)
    assert "Below the 1024 MB warning level" in disk_html(1024 * 1024**2 - 1, 1024)
