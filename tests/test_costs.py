from pathlib import Path

import pytest
from typer.testing import CliRunner

from montaj.cli import app
from montaj.costs import Service, Usage, cost_totals, estimate_cost, log_api_call
from montaj.db import connect, insert
from montaj.models import Clip, ClipStatus, Niche, Source


def test_log_and_report_costs(db_path: Path) -> None:
    audio = Usage(seconds=30)
    tokens = Usage(input_tokens=1000, output_tokens=100)
    audio_cost = estimate_cost(Service.DEEPGRAM, "nova-3", audio)
    token_cost = estimate_cost(Service.CLAUDE, "claude-sonnet-5-5", tokens)
    fake_source = Source(path="fixture.mp4", title="Fixture", niche=Niche.BUSINESS)

    with connect(db_path) as db:
        first = insert(db, fake_source)
        second = insert(db, fake_source)
        assert first.id is not None and second.id is not None
        log_api_call(db, first.id, "transcribe", Service.DEEPGRAM, audio, audio_cost)
        log_api_call(db, first.id, "draft", Service.CLAUDE, tokens, token_cost)
        log_api_call(db, second.id, "draft", Service.CLAUDE, tokens, token_cost)
        insert(
            db, Clip(source_id=first.id, start_s=1, end_s=2, status=ClipStatus.EXPORTED)
        )
        totals = cost_totals(db)
        assert totals.approved_clips == 1
        assert totals.by_stage == pytest.approx(
            {"transcribe": audio_cost, "draft": 2 * token_cost}
        )
        assert totals.by_source[first.id] == pytest.approx(audio_cost + token_cost)
        assert totals.total == pytest.approx(audio_cost + 2 * token_cost)

    result = CliRunner().invoke(app, ["costs", "--source", str(first.id)])
    assert result.exit_code == 0, result.output
    assert f"Per approved clip: ${(audio_cost + token_cost):.6f}" in result.output
    assert f"Source {second.id}:" not in result.output


def test_unknown_model_has_no_estimated_price() -> None:
    with pytest.raises(ValueError, match="Missing price"):
        estimate_cost(Service.CLAUDE, "unknown", Usage(input_tokens=1))


def test_cost_command_distinguishes_attempts_from_reuse(db_path: Path) -> None:
    with connect(db_path) as db:
        for outcome, estimate in (
            ("success", 0.02),
            ("failure", 0.01),
            ("failure", 0.0),
            ("reserved", 0.03),
        ):
            call_id = log_api_call(
                db,
                None,
                "rank",
                Service.CLAUDE,
                Usage(),
                estimate,
            )
            db.execute(
                "UPDATE api_calls SET outcome = ? WHERE id = ?", (outcome, call_id)
            )

    result = CliRunner().invoke(app, ["costs"])
    assert result.exit_code == 0, result.output
    assert "Successful calls: 1 ($0.020000 estimated)" in result.output
    assert "Failed attempts: 2 ($0.010000 estimated)" in result.output
    assert "Unsettled reservations: 1 ($0.030000 estimated)" in result.output
    assert "Estimated total: $0.060000" in result.output
    assert "Cached work creates no API attempt" in result.output
    assert "Per approved clip" not in result.output
