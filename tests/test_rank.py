import json

from montaj.budget import Budget
from montaj.candidates import CandidateSet, SavedPick
from montaj.config import Settings
from montaj.db import connect, fetch, insert
from montaj.models import Campaign, CampaignRules, Clip, ClipStatus, Platform
from montaj.rank import rank_source
from montaj.transcribe import Transcript, Word
from tests.fakes import FakeLLM


def test_filters_before_ranking_and_keeps_top_clips(prepared_source: Settings) -> None:
    directory = prepared_source.data_dir / "sources/1"
    words = [
        Word(text="blocked" if start == 35 else "word", start=start, end=start + 0.4)
        for start in range(100)
    ]
    transcript = Transcript(text="Fixture", language="en", words=words)
    (directory / "transcript.json").write_text(
        transcript.model_dump_json(), encoding="utf-8"
    )
    bounds = [(0, 25), (30, 56), (60, 84), (90, 99)]
    candidates = CandidateSet(
        input_hash="fixture",
        candidates=[
            SavedPick(
                clip_id=index,
                start=start,
                end=end,
                moment_type="story",
                reason="A story",
                hook_idea="A hook",
            )
            for index, (start, end) in enumerate(bounds, 1)
        ],
    )
    (directory / "candidates.json").write_text(
        candidates.model_dump_json(), encoding="utf-8"
    )
    with connect(prepared_source.db_path) as db:
        campaign = insert(
            db,
            Campaign(
                name="Test",
                platform=Platform.YOUTUBE,
                rules_json=CampaignRules(
                    min_seconds=20, max_seconds=60, banned_words=["blocked"]
                ).model_dump_json(),
            ),
        )
        for pick in candidates.candidates:
            insert(
                db,
                Clip(id=pick.clip_id, source_id=1, start_s=pick.start, end_s=pick.end),
            )

    llm = FakeLLM(
        [
            json.dumps(
                [
                    {"clip_id": 1, "score": 4, "reason": "Slow opening"},
                    {"clip_id": 3, "score": 9, "reason": "Strong opening"},
                ]
            )
        ]
    )
    budget = Budget(1, 1)
    result, reused = rank_source(
        prepared_source, 1, campaign_id=campaign.id, top=1, budget=budget, llm=llm
    )
    assert not reused and result.selected_ids == [3]
    prompt_payload = json.loads(llm.prompts[0].split("\n\n")[-1])
    assert [candidate["clip_id"] for candidate in prompt_payload["candidates"]] == [
        1,
        3,
    ]
    with connect(prepared_source.db_path) as db:
        winner = fetch(db, Clip, 3)
        blocked = fetch(db, Clip, 2)
        assert (
            winner is not None
            and winner.status == ClipStatus.RANKED
            and winner.score == 9
        )
        assert blocked is not None and blocked.status == ClipStatus.REJECTED
        assert blocked.decision_reason and "banned" in blocked.decision_reason

    result, reused = rank_source(
        prepared_source, 1, campaign_id=campaign.id, top=2, budget=budget, llm=llm
    )
    assert not reused and result.selected_ids == [3, 1]
    assert len(llm.prompts) == 1
