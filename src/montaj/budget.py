"""Reserve estimated spend before each paid attempt."""

import math
import sqlite3
from dataclasses import dataclass, field
from typing import Annotated
from uuid import uuid4

import typer

from montaj.config import Settings

BudgetOverride = Annotated[
    float | None,
    typer.Option(
        "--budget", min=0, help="Override the run and source spending ceilings in USD."
    ),
]


class BudgetExceeded(RuntimeError):
    """The next paid request would exceed a configured spending ceiling."""


@dataclass(frozen=True, slots=True)
class Budget:
    per_run: float
    per_source: float
    run_id: str = field(default_factory=lambda: uuid4().hex)

    def __post_init__(self) -> None:
        if any(
            not math.isfinite(cap) or cap < 0 for cap in (self.per_run, self.per_source)
        ):
            raise ValueError("Budget ceilings must be finite, non-negative amounts.")

    @classmethod
    def from_settings(
        cls, settings: Settings, override: float | None = None
    ) -> "Budget":
        if override is not None:
            return cls(override, override)
        return cls(settings.max_cost_per_run_usd, settings.max_cost_per_source_usd)


def check_budget(
    db: sqlite3.Connection, budget: Budget, source_id: int, stage: str, estimate: float
) -> None:
    """Compare ceilings under the same write transaction as the reservation."""
    run_spend = db.execute(
        "SELECT COALESCE(SUM(estimated_cost_usd), 0) FROM api_calls WHERE run_id = ?",
        (budget.run_id,),
    ).fetchone()[0]
    source_spend = db.execute(
        "SELECT COALESCE(SUM(estimated_cost_usd), 0) FROM api_calls WHERE source_id = ?",
        (source_id,),
    ).fetchone()[0]
    for label, spent, ceiling in (
        ("per-run", run_spend, budget.per_run),
        ("per-source", source_spend, budget.per_source),
    ):
        if spent + estimate > ceiling + 1e-12:
            raise BudgetExceeded(
                f"{stage}: {label} budget ceiling ${ceiling:.6f}; "
                f"estimated spend ${spent:.6f}, next call ${estimate:.6f}."
            )
