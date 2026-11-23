import json
from io import BytesIO
from pathlib import Path
from urllib.request import Request

import pytest
from pydantic import SecretStr

from montaj.budget import Budget
from montaj.claude import Claude
from montaj.config import Settings
from montaj.costs import Service, Usage, estimate_cost
from montaj.db import connect
from montaj.rank import rank_source
from tests.fakes import FakeLLM
from tests.test_rank import grade, thirty_candidates  # noqa: F401

HAIKU = "claude-haiku-5-5"
SONNET = "claude-sonnet-5-5"


def test_haiku_cost_uses_short_and_long_prompt_tiers() -> None:
    short = Usage(input_tokens=100_000, output_tokens=1_000)
    long = Usage(input_tokens=100_001, output_tokens=1_000)
    assert estimate_cost(Service.CLAUDE, HAIKU, short) == pytest.approx(
        100_000 * 0.10 / 1e6 + 1_000 * 0.50 / 1e6
    )
    assert estimate_cost(Service.CLAUDE, HAIKU, long) == pytest.approx(
        100_001 * 0.50 / 1e6 + 1_000 * 2.50 / 1e6
    )
    assert estimate_cost(Service.CLAUDE, SONNET, long) == pytest.approx(
        100_001 * 2.0 / 1e6 + 1_000 * 10.0 / 1e6
    )


def test_settings_default_ranking_to_haiku_and_leave_other_stages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MONTAJ_RANK_MODEL", raising=False)
    monkeypatch.delenv("MONTAJ_CLAUDE_MODEL", raising=False)
    settings = Settings.from_env()
    assert (settings.rank_model, settings.claude_model) == (HAIKU, SONNET)
    monkeypatch.setenv("MONTAJ_RANK_MODEL", SONNET)
    assert Settings.from_env().rank_model == SONNET


def test_claude_adapter_defaults_to_stage_model_unless_overridden() -> None:
    settings = Settings()
    budget = Budget(1, 1)
    assert Claude(settings, 1, "draft", budget).model == SONNET
    assert Claude(settings, 1, "rank", budget, model=HAIKU).model == HAIKU


def test_rank_routes_to_rank_model_and_isolates_cache(
    thirty_candidates: Settings,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = thirty_candidates.model_copy(
        update={"anthropic_api_key": SecretStr("offline-fake")}
    )
    sent: list[dict[str, object]] = []

    def send(request: Request, *, timeout: int) -> BytesIO:
        assert request.data is not None
        sent.append(json.loads(request.data))
        return BytesIO(
            json.dumps(
                {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps([grade(i) for i in range(1, 31)]),
                        }
                    ],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 2000, "output_tokens": 600},
                }
            ).encode()
        )

    monkeypatch.setattr("montaj.claude.urlopen", send)
    rank_source(settings, 1, campaign_id=None, top=3, budget=Budget(1, 1))
    assert sent[0]["model"] == HAIKU and "thinking" not in sent[0]
    # Same model reuses the paid cache with no new request.
    rank_source(settings, 1, campaign_id=None, top=2, budget=Budget(0, 0))
    assert len(sent) == 1
    # A different ranking model must not reuse Haiku's cached grades.
    other = settings.model_copy(update={"rank_model": SONNET})
    rank_source(other, 1, campaign_id=None, top=3, budget=Budget(1, 1))
    assert sent[1]["model"] == SONNET and sent[1]["thinking"]
    directory = settings.data_dir / "sources/1"
    assert len(list(directory.glob(".rank-*.json"))) == 2
    with connect(settings.db_path) as db:
        costs = [
            row[0]
            for row in db.execute(
                "SELECT estimated_cost_usd FROM api_calls ORDER BY id"
            )
        ]
    assert costs == [
        pytest.approx(2000 * 0.10 / 1e6 + 600 * 0.50 / 1e6),
        pytest.approx(2000 * 2.0 / 1e6 + 600 * 10.0 / 1e6),
    ]


def test_rank_with_fake_llm_makes_no_paid_call(
    thirty_candidates: Settings,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("paid call attempted")

    monkeypatch.setattr("montaj.claude.urlopen", forbidden)
    llm = FakeLLM([json.dumps([grade(i) for i in range(1, 31)])])
    result, _ = rank_source(
        thirty_candidates, 1, campaign_id=None, top=3, budget=Budget(1, 1), llm=llm
    )
    assert len(result.selected_ids) == 3
    assert isinstance(thirty_candidates.data_dir, Path)
