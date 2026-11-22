from dataclasses import replace
from pathlib import Path

import pytest

from montaj.budget import Budget, BudgetExceeded
from montaj.config import Settings
from montaj.costs import Service, Usage, estimate_cost
from montaj.db import connect, insert
from montaj.external import CallContext, CallResult, call_external
from montaj.models import Niche, Source
from tests.fakes import FakeTransport


def test_budget_stops_before_call_and_can_resume(db_path: Path) -> None:
    with connect(db_path) as db:
        source = insert(db, Source(path="source.mp4", title="Test", niche=Niche.GAMES))
        assert source.id is not None
    usage = Usage(seconds=60)
    cost = estimate_cost(Service.DEEPGRAM, "nova-3", usage)
    context = CallContext(
        db_path, source.id, "transcribe", Service.DEEPGRAM, "nova-3", Budget(cost, 1)
    )
    transport = FakeTransport([CallResult("first", usage), CallResult("second", usage)])

    assert call_external(context, usage, lambda: transport.send("first")) == "first"
    with pytest.raises(BudgetExceeded, match="transcribe.*per-run.*estimated spend"):
        call_external(context, usage, lambda: transport.send("blocked"))
    assert transport.requests == ["first"]

    resumed = replace(
        context, budget=Budget.from_settings(Settings(), override=cost * 2)
    )
    assert call_external(resumed, usage, lambda: transport.send("resume")) == "second"

    new_run = replace(context, budget=Budget(1, cost * 2))
    with pytest.raises(BudgetExceeded, match="per-source"):
        call_external(new_run, usage, lambda: transport.send("blocked again"))
    assert transport.requests == ["first", "resume"]
    with connect(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM api_calls").fetchone()[0] == 2


def test_inflight_reservation_counts_against_other_runs(db_path: Path) -> None:
    with connect(db_path) as db:
        source = insert(db, Source(path="source.mp4", title="Test", niche=Niche.GAMES))
        assert source.id is not None
    usage = Usage(seconds=60)
    cost = estimate_cost(Service.DEEPGRAM, "nova-3", usage)
    context = CallContext(
        db_path, source.id, "transcribe", Service.DEEPGRAM, "nova-3", Budget(1, cost)
    )
    other_run = replace(context, budget=Budget(1, cost))

    def first_call() -> CallResult[str]:
        with pytest.raises(BudgetExceeded, match="per-source"):
            call_external(
                other_run,
                usage,
                lambda: pytest.fail("Provider call should be blocked."),
            )
        return CallResult("done", usage)

    assert call_external(context, usage, first_call) == "done"
