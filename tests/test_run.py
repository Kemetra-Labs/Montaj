import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from montaj.budget import Budget
from montaj.cli import app
from montaj.config import Settings
from montaj.db import connect, fetch, insert
from montaj.models import Campaign, CampaignRules, Clip, ClipStatus, Platform
from montaj.run import STAGES, RunError, run_source
from montaj.transcribe import Transcript, Word
from tests.fakes import FakeLLM


class _Backend:
    def __init__(self) -> None:
        self.calls = 0

    def transcribe(self, audio_path: Path) -> Transcript:
        del audio_path
        self.calls += 1
        words = [
            Word(text="Fin." if index == 12 else "mot", start=index, end=index + 0.5)
            for index in range(18)
        ]
        return Transcript(
            text=" ".join(word.text for word in words), language="en", words=words
        )


class _Renderer:
    def render(self, video: Path, output: Path, start: float, end: float) -> None:
        del video, start, end
        output.write_bytes(b"raw")


def _silence_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_tool(tool: str, args: list[str], *, cwd: Path | None = None) -> str:
        del tool, args
        assert cwd is not None
        (cwd / "final.mp4").write_bytes(b"captioned")
        return ""

    monkeypatch.setattr("montaj.captions.run_tool", fake_tool)


def _campaign(settings: Settings) -> int:
    rules = CampaignRules(min_seconds=1, max_seconds=60, required_hashtags=["#Clip"])
    with connect(settings.db_path) as db:
        campaign = insert(
            db,
            Campaign(
                name="Fixture",
                platform=Platform.YOUTUBE,
                rules_json=rules.model_dump_json(),
            ),
        )
        assert campaign.id is not None
        db.execute("UPDATE sources SET campaign_id = ? WHERE id = 1", (campaign.id,))
    return campaign.id


def _draft() -> str:
    return json.dumps(
        {
            "hooks": [
                {"text": "Why did the ship jump?", "style": "question"},
                {"text": "The ship jumped early", "style": "bold_claim"},
            ],
            "caption": "The ship left early. #Clip",
            "hashtags": ["#Clip"],
        }
    )


class _Model(FakeLLM):
    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if prompt.startswith("# Candidate"):
            return json.dumps(
                [
                    {
                        "start": 3.1,
                        "end": 12,
                        "moment_type": "story",
                        "reason": "Stands alone",
                        "hook_idea": "A hook",
                    }
                ]
            )
        if prompt.startswith("# Rank"):
            clip_id = int(re.search(r'"clip_id":(\d+)', prompt).group(1))  # type: ignore[union-attr]
            return json.dumps([{"clip_id": clip_id, "score": 8, "reason": "Strong"}])
        if prompt.startswith("# Draft"):
            return _draft()
        raise AssertionError(prompt[:40])


def _run(
    settings: Settings,
    backend: _Backend,
    llm: _Model | None,
    *,
    until: str = "check",
    campaign_id: int,
) -> list[str]:
    reports = run_source(
        settings,
        1,
        campaign_id=campaign_id,
        until=until,
        budget=Budget(1, 1),
        min_seconds=5,
        max_seconds=15,
        backend=backend,
        llm=llm,
        renderer=_Renderer(),
    )
    return [report.line() for report in reports]


def test_run_walks_every_stage_then_skips_current_output(
    prepared_source: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _silence_ffmpeg(monkeypatch)
    campaign_id = _campaign(prepared_source)
    backend = _Backend()
    model = _Model([])
    first = _run(prepared_source, backend, model, campaign_id=campaign_id)
    assert [line.split(":", 1)[0] for line in first] == list(STAGES)
    assert "transcribed" in first[0]
    assert "1 checked" in first[-1]
    assert backend.calls == 1
    with connect(prepared_source.db_path) as db:
        clip = fetch(db, Clip, 1)
    assert clip is not None
    assert clip.status == ClipStatus.CHECKED
    assert clip.hook_a == "Why did the ship jump?"

    second = _run(prepared_source, backend, model, campaign_id=campaign_id)
    assert "reused" in second[0]
    assert backend.calls == 1
    assert len(model.prompts) == 3


def test_run_stops_and_resumes_after_a_failed_stage(prepared_source: Settings) -> None:
    campaign_id = _campaign(prepared_source)
    backend = _Backend()

    class Boom:
        def complete(self, prompt: str) -> str:
            del prompt
            raise RuntimeError("candidates broke")

    with pytest.raises(RunError, match="candidates: candidates broke"):
        run_source(
            prepared_source,
            1,
            campaign_id=campaign_id,
            budget=Budget(1, 1),
            min_seconds=5,
            max_seconds=15,
            backend=backend,
            llm=Boom(),
        )
    assert backend.calls == 1
    resumed = _run(
        prepared_source,
        backend,
        _Model([]),
        until="candidates",
        campaign_id=campaign_id,
    )
    assert "reused" in resumed[0]
    assert "found" in resumed[1]
    assert backend.calls == 1


def test_run_until_captions_does_not_draft(
    prepared_source: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _silence_ffmpeg(monkeypatch)
    campaign_id = _campaign(prepared_source)
    model = _Model([])
    lines = _run(
        prepared_source, _Backend(), model, until="captions", campaign_id=campaign_id
    )
    assert [line.split(":", 1)[0] for line in lines] == list(STAGES[:5])
    assert len(model.prompts) == 2
    with connect(prepared_source.db_path) as db:
        clip = fetch(db, Clip, 1)
    assert clip is not None and clip.hook_a is None


def test_unknown_until_is_rejected(db_path: Path) -> None:
    result = CliRunner().invoke(app, ["run", "1", "--until", "export"])
    assert result.exit_code == 1
    assert "Unknown stage" in result.output
