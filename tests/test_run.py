import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from functools import partial
from pathlib import Path

import pytest
from typer.testing import CliRunner

from montaj.budget import Budget
from montaj.cli import app
from montaj.config import Settings
from montaj.db import connect, fetch, insert
from montaj.job import produce_batch
from montaj.models import Campaign, CampaignRules, Clip, ClipStatus, Platform
from montaj.review import review_decision
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


def test_job_backfills_failed_checks_and_reuses_paid_work(
    prepared_source: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _silence_ffmpeg(monkeypatch)
    _campaign(prepared_source)
    with connect(prepared_source.db_path) as db:
        db.execute(
            "UPDATE sources SET allowed_basis = 'fixture permission' WHERE id = 1"
        )

    class BatchModel(_Model):
        def complete(self, prompt: str) -> str:
            self.prompts.append(prompt)
            if prompt.startswith("# Candidate"):
                assert '"min_seconds":1.0' in prompt
                return json.dumps(
                    [
                        {
                            "start": start,
                            "end": end,
                            "moment_type": "story",
                            "reason": "Stands alone",
                            "hook_idea": "A hook",
                        }
                        for start, end in [(1, 4), (8, 12)]
                    ]
                )
            if prompt.startswith("# Rank"):
                return json.dumps(
                    [
                        {"clip_id": int(clip_id), "score": score, "reason": "Strong"}
                        for clip_id, score in zip(
                            re.findall(r'"clip_id":(\d+)', prompt), [9, 8]
                        )
                    ]
                )
            if prompt.startswith("# Draft"):
                return (
                    _draft().replace("#Clip", "#Other")
                    if len(self.prompts) == 3
                    else _draft()
                )
            raise AssertionError(prompt[:40])

    backend, model = _Backend(), BatchModel([])
    for _ in range(2):
        result = produce_batch(
            prepared_source,
            1,
            top=1,
            budget=Budget(1, 1),
            backend=backend,
            llm=model,
            renderer=_Renderer(),
        )
        assert result.ready == [2]
        assert result.failures == 1 and result.stop is None
    assert backend.calls == 1 and len(model.prompts) == 4
    with connect(prepared_source.db_path) as db:
        failed = fetch(db, Clip, 1)
        ready = fetch(db, Clip, 2)
    assert failed is not None and failed.check_input_hash
    assert ready is not None and ready.check_input_hash
    assert "cannot be approved" in review_decision(
        prepared_source, 1, 1, action="approve", version=failed.check_input_hash
    )
    with pytest.raises(ValueError, match="Refresh"):
        review_decision(prepared_source, 1, 2, action="approve", version="stale")
    assert "Clip 2 approved" in review_decision(
        prepared_source, 1, 2, action="approve", version=ready.check_input_hash
    )
    resumed = produce_batch(
        prepared_source,
        1,
        top=1,
        budget=Budget(1, 1),
        backend=backend,
        llm=model,
        renderer=_Renderer(),
    )
    assert resumed.ready == [2] and resumed.previously_approved == 1
    assert ready.final_path
    (prepared_source.data_dir / ready.final_path).unlink()
    resumed = produce_batch(
        prepared_source,
        1,
        top=1,
        budget=Budget(1, 1),
        backend=backend,
        llm=model,
        renderer=_Renderer(),
    )
    assert resumed.ready == [] and resumed.previously_approved == 0 and resumed.stop
    with connect(prepared_source.db_path) as db:
        stored = fetch(db, Clip, 2)
    assert stored is not None and stored.status == ClipStatus.APPROVED


def test_job_reloads_the_campaign_after_acquiring_the_source_lock(
    prepared_source: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _campaign(prepared_source)
    with connect(prepared_source.db_path) as db:
        db.execute(
            "UPDATE sources SET allowed_basis = 'fixture permission' WHERE id = 1"
        )

    @contextmanager
    def unlink_just_before_lock(path: Path, *, command: str) -> Iterator[None]:
        with connect(prepared_source.db_path) as db:
            db.execute("UPDATE sources SET campaign_id = NULL WHERE id = 1")
        yield

    monkeypatch.setattr("montaj.job.source_lock", unlink_just_before_lock)
    backend = _Backend()
    with pytest.raises(ValueError, match="needs a campaign"):
        produce_batch(prepared_source, 1, top=1, budget=Budget(1, 1), backend=backend)
    assert backend.calls == 0


def test_job_uses_the_permission_and_budget_guards(
    prepared_source: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _campaign(prepared_source)
    runner = CliRunner()
    refused = runner.invoke(app, ["job", "--resume", "1"])
    assert refused.exit_code == 1 and "permission basis" in refused.output
    refused = runner.invoke(
        app, ["job", "example.mp4", "--campaign", "1", "--niche", "games"]
    )
    assert refused.exit_code == 1 and "Only permitted sources" in refused.output
    with connect(prepared_source.db_path) as db:
        db.execute(
            "UPDATE sources SET allowed_basis = 'fixture permission' WHERE id = 1"
        )
    monkeypatch.setenv("DEEPGRAM_API_KEY", "unused-in-test")
    stopped = runner.invoke(app, ["job", "--resume", "1", "--budget", "0"])
    assert stopped.exit_code == 0
    assert "0/10 clips" in stopped.output and "budget ceiling" in stopped.output
    with connect(prepared_source.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM api_calls").fetchone()[0] == 0


@pytest.mark.media
def test_job_cli_imports_and_renders_an_english_video(
    db_path: Path, media_fixture_dir: Path, ffmpeg: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MONTAJ_MIN_FREE_MB", "0")
    campaign = _campaign(Settings(data_dir=db_path.parent))
    monkeypatch.setattr(
        "montaj.job.produce_batch",
        partial(produce_batch, backend=_Backend(), llm=_Model([])),
    )
    result = CliRunner().invoke(
        app,
        [
            "job",
            str(media_fixture_dir / "sample.mp4"),
            "--campaign",
            str(campaign),
            "--niche",
            "games",
            "--allowed",
            "--basis",
            "fixture permission",
            "--top",
            "1",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "1/1 clips ready" in result.output
    assert "/?source=1" in result.output
    with connect(db_path) as db:
        clip = fetch(db, Clip, 1)
    assert clip is not None and clip.status == ClipStatus.CHECKED
    assert clip.final_path and (db_path.parent / clip.final_path).stat().st_size > 0
