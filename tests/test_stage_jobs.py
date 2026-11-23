from typing import Any

import pytest

from montaj import stage_jobs
from montaj.budget import Budget, BudgetExceeded
from montaj.check import ClipCheck
from montaj.config import Settings
from montaj.db import connect, insert
from montaj.models import (
    Campaign,
    CampaignRules,
    CheckResult,
    Clip,
    ClipStatus,
    Platform,
)
from montaj.rank import RankedClip, RankSet
from montaj.stage_jobs import (
    PAID_STAGES,
    BoardStage,
    StageResult,
    run_board_stage,
)

_BUDGET = Budget(1.0, 1.0)


class Recorder:
    """Stand-ins for the stage functions that note their calls in order."""

    def __init__(self, outcomes: dict[int, CheckResult] | None = None) -> None:
        self.calls: list[tuple[str, int | None]] = []
        self.outcomes = outcomes or {}

    def stage(self, name: str, clip_id: int | None) -> None:
        self.calls.append((name, clip_id))

    def transcribe(self, settings: Settings, source_id: int, **kwargs: Any) -> None:
        assert kwargs["language"] == "en"
        self.stage("transcribe", None)

    def candidates(self, settings: Settings, source_id: int, **kwargs: Any) -> None:
        assert (kwargs["bounds"].min_seconds, kwargs["bounds"].max_seconds) == (15, 60)
        self.stage("candidates", None)

    def rank(
        self, settings: Settings, source_id: int, **kwargs: Any
    ) -> tuple[RankSet, bool]:
        assert (kwargs["campaign_id"], kwargs["top"]) == (1, 30)
        self.stage("rank", None)
        clips = [
            RankedClip(clip_id=1, score=8, reason="a", selected=True),
            RankedClip(clip_id=2, score=3, reason="b", selected=False),
        ]
        return RankSet(input_hash="h", clips=clips), True

    def cut(self, settings: Settings, source_id: int, clip_id: int, **_: Any) -> None:
        self.stage("cut", clip_id)

    def caption(self, settings: Settings, source_id: int, clip_id: int) -> None:
        self.stage("captions", clip_id)

    def draft(self, settings: Settings, source_id: int, **kwargs: Any) -> None:
        self.stage("draft", kwargs["clip_id"])

    def check(
        self, settings: Settings, source_id: int, *, clip_id: int
    ) -> list[tuple[Clip, ClipCheck]]:
        self.stage("check", clip_id)
        result = self.outcomes.get(clip_id, CheckResult.PASS)
        clip = Clip(id=clip_id, source_id=source_id, start_s=0, end_s=5)
        return [(clip, ClipCheck(result, (), False))]


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    fake = Recorder()
    monkeypatch.setattr(stage_jobs, "transcribe_source", fake.transcribe)
    monkeypatch.setattr(stage_jobs, "find_candidates", fake.candidates)
    monkeypatch.setattr(stage_jobs, "rank_source", fake.rank)
    monkeypatch.setattr(stage_jobs, "cut_source", fake.cut)
    monkeypatch.setattr(stage_jobs, "caption_source", fake.caption)
    monkeypatch.setattr(stage_jobs, "draft_source", fake.draft)
    monkeypatch.setattr(stage_jobs, "check_source", fake.check)
    return fake


@pytest.fixture
def batch(prepared_source: Settings) -> Settings:
    """Source 1 on a campaign, with clips 1 to 5 ranked and selected."""
    rules = CampaignRules(min_seconds=15, max_seconds=60)
    with connect(prepared_source.db_path) as db:
        insert(
            db,
            Campaign(
                id=1,
                name="Fixture",
                platform=Platform.YOUTUBE,
                rules_json=rules.model_dump_json(),
            ),
        )
        db.execute("UPDATE sources SET campaign_id = 1 WHERE id = 1")
        for clip_id in range(1, 6):
            insert(
                db,
                Clip(
                    id=clip_id,
                    source_id=1,
                    start_s=0,
                    end_s=5,
                    status=ClipStatus.RANKED,
                ),
            )
    _save_rank(prepared_source, [1, 2, 3, 4, 5])
    return prepared_source


def _save_rank(settings: Settings, selected: list[int]) -> None:
    clips = [
        RankedClip(clip_id=clip_id, score=8, reason="r", selected=True)
        for clip_id in selected
    ]
    rank = RankSet(input_hash="h", clips=clips)
    (settings.data_dir / "sources/1/rank.json").write_text(
        rank.model_dump_json(), encoding="utf-8"
    )


def _set_status(settings: Settings, clip_id: int, status: ClipStatus) -> None:
    with connect(settings.db_path) as db:
        db.execute("UPDATE clips SET status = ? WHERE id = ?", (status.value, clip_id))


def _run(
    settings: Settings,
    stage: BoardStage,
    progress: list[str],
    *,
    top: int = 10,
    retry_failed: bool = False,
) -> StageResult:
    return run_board_stage(
        settings,
        1,
        stage,
        top=top,
        budget=_BUDGET,
        progress=progress.append,
        retry_failed=retry_failed,
    )


def test_paid_stages_are_transcribe_rank_and_draft() -> None:
    assert PAID_STAGES == {BoardStage.TRANSCRIBE, BoardStage.RANK, BoardStage.DRAFT}


def test_transcribe_stage_runs_transcription_only(
    batch: Settings, recorder: Recorder
) -> None:
    progress: list[str] = []
    result = _run(batch, BoardStage.TRANSCRIBE, progress)
    assert recorder.calls == [("transcribe", None)]
    assert progress == ["Transcribing English audio"]
    assert result == StageResult(summary="Transcribed source 1.")


def test_rank_stage_runs_candidates_then_rank_in_order(
    batch: Settings, recorder: Recorder
) -> None:
    progress: list[str] = []
    result = _run(batch, BoardStage.RANK, progress)
    assert recorder.calls == [("candidates", None), ("rank", None)]
    assert progress == ["Finding candidates", "Ranking candidates"]
    assert result.summary == "Ranked 2 candidates and selected 1."
    assert result.problem == ""


def test_render_stage_touches_only_selected_clips_up_to_top(
    batch: Settings, recorder: Recorder
) -> None:
    progress: list[str] = []
    result = _run(batch, BoardStage.RENDER, progress, top=2)
    assert recorder.calls == [
        ("cut", 1),
        ("captions", 1),
        ("cut", 2),
        ("captions", 2),
    ]
    assert progress == [
        "Rendering clip 1",
        "Captioning clip 1",
        "Rendering clip 2",
        "Captioning clip 2",
    ]
    assert result.summary == "Rendered and captioned 2 clips."


def test_render_stage_only_uses_the_saved_selection(
    batch: Settings, recorder: Recorder
) -> None:
    _save_rank(batch, [4, 2])
    _run(batch, BoardStage.RENDER, [])
    assert [call for call in recorder.calls if call[0] == "cut"] == [
        ("cut", 4),
        ("cut", 2),
    ]


def test_render_stage_requires_a_saved_ranking(
    batch: Settings, recorder: Recorder
) -> None:
    (batch.data_dir / "sources/1/rank.json").unlink()
    with pytest.raises(ValueError, match="Run rank first."):
        _run(batch, BoardStage.RENDER, [])
    assert recorder.calls == []


def test_top_must_be_within_the_candidate_limit(
    batch: Settings, recorder: Recorder
) -> None:
    with pytest.raises(ValueError, match="between 1 and 30"):
        _run(batch, BoardStage.RENDER, [], top=0)
    assert recorder.calls == []


def test_retry_failed_only_touches_needs_fix_clips(
    batch: Settings, recorder: Recorder
) -> None:
    _set_status(batch, 2, ClipStatus.NEEDS_FIX)
    _set_status(batch, 4, ClipStatus.NEEDS_FIX)
    result = _run(batch, BoardStage.RENDER, [], top=3, retry_failed=True)
    assert [call for call in recorder.calls if call[0] == "cut"] == [
        ("cut", 2),
        ("cut", 4),
    ]
    assert result.summary == "Rendered and captioned 2 clips."


def test_retry_with_nothing_failed_reports_it(
    batch: Settings, recorder: Recorder
) -> None:
    result = _run(batch, BoardStage.RENDER, [], retry_failed=True)
    assert recorder.calls == []
    assert result.summary == "No clips to render."


def test_approved_clips_are_never_rerun(batch: Settings, recorder: Recorder) -> None:
    _set_status(batch, 2, ClipStatus.APPROVED)
    _set_status(batch, 3, ClipStatus.EXPORTED)
    result = _run(batch, BoardStage.RENDER, [], top=4)
    assert [call for call in recorder.calls if call[0] == "cut"] == [
        ("cut", 1),
        ("cut", 4),
    ]
    assert result.summary == "Rendered and captioned 2 clips."


def test_draft_stage_runs_draft_then_check_and_reports_failures(
    batch: Settings, recorder: Recorder
) -> None:
    recorder.outcomes = {2: CheckResult.FAIL, 3: CheckResult.WARN}
    progress: list[str] = []
    result = _run(batch, BoardStage.DRAFT, progress, top=3)
    assert recorder.calls == [
        ("draft", 1),
        ("check", 1),
        ("draft", 2),
        ("check", 2),
        ("draft", 3),
        ("check", 3),
    ]
    assert progress[:2] == ["Drafting clip 1", "Checking clip 1"]
    assert result.summary == (
        "Drafted and checked 3 clips: 1 passed, 1 warning, 1 failed (clip 2)."
    )
    assert result.problem == ""


def test_budget_exceeded_returns_problem_not_exception(
    batch: Settings, monkeypatch: pytest.MonkeyPatch, recorder: Recorder
) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise BudgetExceeded("Run budget of $1.00 would be exceeded.")

    monkeypatch.setattr(stage_jobs, "draft_source", refuse)
    result = _run(batch, BoardStage.DRAFT, [])
    assert result.problem == "Run budget of $1.00 would be exceeded."
    assert "stopped" in result.summary.lower()
    assert recorder.calls == []


def test_budget_exceeded_while_ranking_is_reported(
    batch: Settings, monkeypatch: pytest.MonkeyPatch, recorder: Recorder
) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise BudgetExceeded("Source budget reached.")

    monkeypatch.setattr(stage_jobs, "find_candidates", refuse)
    result = _run(batch, BoardStage.RANK, [])
    assert result.problem == "Source budget reached."
    assert recorder.calls == []


def test_rank_stage_needs_a_campaign(batch: Settings, recorder: Recorder) -> None:
    with connect(batch.db_path) as db:
        db.execute("UPDATE sources SET campaign_id = NULL WHERE id = 1")
    with pytest.raises(ValueError, match="needs a campaign"):
        _run(batch, BoardStage.RANK, [])
    assert recorder.calls == []


def test_source_without_permission_is_refused(
    batch: Settings, recorder: Recorder
) -> None:
    with connect(batch.db_path) as db:
        db.execute("UPDATE sources SET source_allowed = 0 WHERE id = 1")
    with pytest.raises(Exception, match="lacks permission"):
        _run(batch, BoardStage.TRANSCRIBE, [])
    assert recorder.calls == []


def test_clip_selection_never_calls_rank_source(
    batch: Settings, monkeypatch: pytest.MonkeyPatch, recorder: Recorder
) -> None:
    def spend(*args: object, **kwargs: object) -> None:
        pytest.fail("A render must not call rank_source.")

    monkeypatch.setattr(stage_jobs, "rank_source", spend)
    _run(batch, BoardStage.RENDER, [], top=1)
    assert recorder.calls == [("cut", 1), ("captions", 1)]


def test_rank_json_with_missing_clip_row_is_an_error(
    batch: Settings, recorder: Recorder
) -> None:
    _save_rank(batch, [1, 99])
    with pytest.raises(ValueError, match="Ranked clip 99 is missing"):
        _run(batch, BoardStage.RENDER, [])
    assert recorder.calls == []
