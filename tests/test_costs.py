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
