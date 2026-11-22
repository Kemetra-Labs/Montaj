"""Estimate and report the USD cost of pipeline API calls."""

import sqlite3
import tomllib
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field

from montaj.models import utc_now


class Service(StrEnum):
    DEEPGRAM = "deepgram"
    CLAUDE = "claude"


class Usage(BaseModel):
    seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)


def estimate_cost(
    service: Service, model: str, usage: Usage, prices_path: Path | None = None
) -> float:
    path = prices_path or Path(__file__).with_name("prices.toml")
    with path.open("rb") as prices_file:
        prices = tomllib.load(prices_file)
    try:
        rates = prices[service][model]
        if service == Service.DEEPGRAM:
            return (usage.seconds or 0) * float(rates["usd_per_minute"]) / 60
        return (
            (usage.input_tokens or 0) * float(rates["usd_per_million_input_tokens"])
            + (usage.output_tokens or 0) * float(rates["usd_per_million_output_tokens"])
        ) / 1_000_000
    except KeyError as exc:
        raise ValueError(
            f"Missing price for {service} model {model} in {path}."
        ) from exc


def log_api_call(
    db: sqlite3.Connection,
    source_id: int,
    stage: str,
    service: Service,
    usage: Usage,
    estimated_cost_usd: float,
) -> None:
    db.execute(
        """INSERT INTO api_calls (source_id, stage, service, input_tokens, output_tokens,
           seconds, estimated_cost_usd, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            source_id,
            stage,
            service,
            usage.input_tokens,
            usage.output_tokens,
            usage.seconds,
            estimated_cost_usd,
            utc_now().isoformat(),
        ),
    )


@dataclass(slots=True)
class CostTotals:
    by_stage: dict[str, float] = field(default_factory=dict)
    by_source: dict[int, float] = field(default_factory=dict)
    approved_clips: int = 0

    @property
    def total(self) -> float:
        return sum(self.by_stage.values())


def cost_totals(db: sqlite3.Connection, source_id: int | None = None) -> CostTotals:
    summary = CostTotals()
    rows = db.execute(
        """SELECT source_id, stage, SUM(estimated_cost_usd) AS total FROM api_calls
           WHERE (? IS NULL OR source_id = ?) GROUP BY source_id, stage
           ORDER BY source_id, stage""",
        (source_id, source_id),
    )
    for row in rows:
        source, stage, total = row
        summary.by_source[source] = summary.by_source.get(source, 0) + total
        summary.by_stage[stage] = summary.by_stage.get(stage, 0) + total
    approved = db.execute(
        """SELECT COUNT(*) FROM clips WHERE (? IS NULL OR source_id = ?)
           AND status IN ('approved', 'exported', 'posted')""",
        (source_id, source_id),
    ).fetchone()
    summary.approved_clips = int(approved[0])
    return summary
