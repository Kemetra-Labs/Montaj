"""The New job panel on the board home: start a job from a permitted video."""

from html import escape

from montaj.candidates import CANDIDATE_LIMIT
from montaj.dashboard_parts import JOBS_LINK, BoardContext, hidden
from montaj.job import DEFAULT_TARGET
from montaj.models import Niche

_NO_CAMPAIGN = "Create a campaign with <code>montaj campaign add</code> to start a job."
_BUSY = "The worker is preparing a batch. You can review other sources while it runs."


def _limits_fields() -> str:
    return (
        f'<label>Target clips <input name="top" type="number" min="1" max="{CANDIDATE_LIMIT}" '
        f'value="{DEFAULT_TARGET}" required></label>'
        '<label>Budget override (USD, optional) <input name="budget" type="number" min="0" step="0.01"></label>'
    )


def _campaign_options(choices: tuple[tuple[int, str], ...]) -> str:
    return "".join(
        f'<option value="{campaign_id}">{escape(name)}</option>'
        for campaign_id, name in choices
    )


def _niche_options() -> str:
    return "".join(
        f'<option value="{niche.value}">{niche.value.capitalize()}</option>'
        for niche in Niche
    )


def _form_html(ctx: BoardContext) -> str:
    return f"""<form class="job-form" action="{JOBS_LINK}" method="post">
{hidden(token=ctx.token)}
<label>Video <input name="location" maxlength="2048" required placeholder="YouTube URL or inbox filename"></label>
<label>Campaign <select name="campaign" required>{_campaign_options(ctx.campaign_choices)}</select></label>
<label>Niche <select name="niche" required>{_niche_options()}</select></label>
<label>Permission basis <input name="basis" maxlength="200" required></label>
<label class="inline-field"><input type="checkbox" name="allowed" value="true" required> I have permission to use this video</label>
{_limits_fields()}
<button class="button primary">Start job</button></form>"""


def _body_html(ctx: BoardContext) -> str:
    if ctx.running is not None:
        return f"<p>{_BUSY}</p>"
    if not ctx.campaign_choices:
        return f"<p>{_NO_CAMPAIGN}</p>"
    return _form_html(ctx)


def new_job_html(ctx: BoardContext) -> str:
    """The collapsible form. Empty on a read-only server. Open when a message waits."""
    if not ctx.operations:
        return ""
    state = " open" if ctx.notice else ""
    return (
        f'<details id="new-job" class="panel job-panel"{state}>'
        f"<summary>New job</summary>{_body_html(ctx)}</details>"
    )
