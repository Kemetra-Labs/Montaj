import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from montaj.budget import Budget
from montaj.candidates import CandidateBounds, find_candidates
from montaj.cli import app
from montaj.config import Settings
from montaj.cut import selected_clips
from montaj.db import connect, fetch, insert, save_clip
from montaj.models import Campaign, Clip, ClipStatus, Platform, Source, utc_now
from montaj.rank import rank_source
from montaj.transcribe import Transcript, Word
from tests.fakes import FakeLLM


def _rules(objective: str | None = None, **extra: object) -> str:
    brief = {
        "objective": objective,
        "target_audience": "New creators",
        "key_messages": ["Start small"],
        "content_guidance": ["Prefer actionable advice"],
    }
    rules: dict[str, object] = {"min_seconds": 20, "max_seconds": 60, **extra}
    if objective is not None:
        rules["brief"] = brief
    return json.dumps(rules)


def _campaign(settings: Settings, campaign_id: int, rules: str) -> None:
    with connect(settings.db_path) as db:
        insert(
            db,
            Campaign(
                id=campaign_id, name="Test", platform=Platform.YOUTUBE, rules_json=rules
            ),
        )


def _link(campaign_id: int, expected_exit: int = 0) -> str:
    result = CliRunner().invoke(
        app, ["campaign", "link-source", "1", "--campaign", str(campaign_id)]
    )
    assert result.exit_code == expected_exit, result.output
    return result.output


def test_link_enforces_existing_source_rules(prepared_source: Settings) -> None:
    _campaign(prepared_source, 1, _rules(allowed_sources=["other.mp4"]))
    assert "allowed-source list" in _link(1, expected_exit=2)
    with connect(prepared_source.db_path) as db:
        source = fetch(db, Source, 1)
        assert source is not None and source.campaign_id is None


def test_link_and_brief_changes_refresh_selection(
    prepared_source: Settings, tmp_path: Path
) -> None:
    settings = prepared_source
    directory = settings.data_dir / "sources/1"
    transcript = Transcript(
        text="Advice",
        language="en",
        words=[Word(text="advice", start=i, end=i + 0.5) for i in range(35)],
    ).model_dump_json()
    (directory / "transcript.json").write_text(transcript, encoding="utf-8")
    _campaign(settings, 1, _rules())
    _campaign(settings, 2, _rules("Reach subscribers"))
    _campaign(settings, 3, _rules("Grow awareness"))
    pick = '[{"start":0,"end":25,"moment_type":"story","reason":"Advice","hook_idea":"Start"}]'
    grade = '[{"clip_id":1,"score":8,"reason":"Relevant advice"}]'
    discovery = FakeLLM([pick] * 4)
    ranking = FakeLLM([grade] * 3)
    budget = Budget(1, 1)

    def discover() -> bool:
        return find_candidates(
            settings, 1, bounds=CandidateBounds(), budget=budget, llm=discovery
        )[1]

    def rank() -> bool:
        return rank_source(
            settings, 1, campaign_id=None, top=1, budget=budget, llm=ranking
        )[1]

    assert not discover()
    assert "campaign_brief" not in discovery.prompts[-1]
    _link(1)
    assert discover()
    _link(2)
    with pytest.raises(ValueError, match="Rerun candidates"):
        rank()
    assert not discover() and discover()
    assert "Reach subscribers" in discovery.prompts[-1]
    assert "New creators" in discovery.prompts[-1]
    assert not rank() and rank()
    assert "Reach subscribers" in ranking.prompts[-1]

    with connect(settings.db_path) as db:
        approved = fetch(db, Clip, 1)
        assert approved is not None
        approved.status = ClipStatus.APPROVED
        approved.reviewed_at = utc_now()
        save_clip(db, approved)

    path = tmp_path / "rules.json"
    path.write_text(_rules("Increase signups"), encoding="utf-8")
    result = CliRunner().invoke(
        app, ["campaign", "update-rules", "2", "--rules", str(path)]
    )
    assert result.exit_code == 0, result.output
    with pytest.raises(ValueError, match="Rerun candidates"):
        rank()
    with pytest.raises(ValueError, match="Rerun"):
        selected_clips(settings, 1, directory)
    assert not discover()
    assert "Increase signups" in discovery.prompts[-1]
    assert not rank()
    assert "Increase signups" in ranking.prompts[-1]
    assert selected_clips(settings, 1, directory) == []
    assert selected_clips(settings, 1, directory, clip_id=1) == [approved]
    _link(3)
    assert not discover()
    assert "Grow awareness" in discovery.prompts[-1]
    assert (directory / "transcript.json").read_text(encoding="utf-8") == transcript
    with connect(settings.db_path) as db:
        assert fetch(db, Clip, 1) == approved


def test_update_rejects_blank_objective(
    prepared_source: Settings, tmp_path: Path
) -> None:
    _campaign(prepared_source, 1, _rules())
    path = tmp_path / "rules.json"
    path.write_text(_rules(" "), encoding="utf-8")
    result = CliRunner().invoke(
        app, ["campaign", "update-rules", "1", "--rules", str(path)]
    )
    assert result.exit_code == 2
    assert "brief.objective" in result.output
