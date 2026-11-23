import json
from io import BytesIO
from typing import cast
from urllib.request import Request

import pytest
from pydantic import SecretStr

from montaj.budget import Budget, BudgetExceeded
from montaj.candidates import CandidateSet, SavedPick
from montaj.config import Settings
from montaj.costs import cost_totals
from montaj.cut import selected_clips
from montaj.db import connect, fetch, insert, save_clip
from montaj.external import ExternalCallError
from montaj.models import Campaign, CampaignRules, Clip, ClipStatus, Platform, utc_now
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


@pytest.fixture
def thirty_candidates(prepared_source: Settings) -> Settings:
    directory = prepared_source.data_dir / "sources/1"
    words = [
        Word(text="Fixture", start=index * 30, end=index * 30 + 1)
        for index in range(30)
    ]
    (directory / "transcript.json").write_text(
        Transcript(text="Fixture", language="en", words=words).model_dump_json(),
        encoding="utf-8",
    )
    picks = [
        SavedPick(
            clip_id=index + 1,
            start=word.start,
            end=word.start + 25,
            moment_type="story",
            reason="A story",
            hook_idea="A hook",
        )
        for index, word in enumerate(words)
    ]
    (directory / "candidates.json").write_text(
        CandidateSet(input_hash="fixture", candidates=picks).model_dump_json(),
        encoding="utf-8",
    )
    with connect(prepared_source.db_path) as db:
        for pick in picks:
            insert(
                db,
                Clip(id=pick.clip_id, source_id=1, start_s=pick.start, end_s=pick.end),
            )
    return prepared_source


def grade(clip_id: int, score: float = 5) -> dict[str, object]:
    return {"clip_id": clip_id, "score": score, "reason": "Strong opening"}


def test_thirty_grades_share_one_global_ranking_and_reuse_cache(
    thirty_candidates: Settings,
) -> None:
    answers = [grade(i, {5: 9, 19: 10, 28: 9}.get(i, 5)) for i in range(30, 0, -1)]
    llm = FakeLLM([json.dumps(answers)])
    with connect(thirty_candidates.db_path) as db:
        approved = fetch(db, Clip, 2)
        assert approved is not None
        approved.status = ClipStatus.APPROVED
        approved.reviewed_at = utc_now()
        approved.score = 7
        save_clip(db, approved)
    result, reused = rank_source(
        thirty_candidates, 1, campaign_id=None, top=3, budget=Budget(1, 1), llm=llm
    )
    assert not reused and result.selected_ids == [19, 5, 28] and len(result.clips) == 30
    assert len(json.loads(llm.prompts[0].split("\n\n")[-1])["candidates"]) == 30
    result, reused = rank_source(
        thirty_candidates, 1, campaign_id=None, top=2, budget=Budget(0, 0), llm=llm
    )
    assert not reused and result.selected_ids == [19, 5] and len(llm.prompts) == 1
    with connect(thirty_candidates.db_path) as db:
        assert fetch(db, Clip, 2) == approved


@pytest.mark.parametrize(
    ("response", "message"),
    [
        ('[{"clip_id":1,"reason":"private-completion', "invalid or incomplete JSON"),
        (json.dumps([grade(1)] * 30), "duplicate candidate IDs"),
        (json.dumps([grade(i) for i in range(1, 30)]), "missing candidate IDs"),
        (
            json.dumps([grade(i) for i in range(1, 30)] + [grade(999)]),
            "unexpected candidate IDs",
        ),
        (json.dumps([grade(1, 11)]), "invalid grade score"),
        (
            '[{"clip_id":1,"score":"9","reason":"unexpected type"}]',
            "invalid grade score",
        ),
        ('[{"clip_id":1,"score":5,"reason":"   "}]', "invalid grade reason"),
        ('{"grades": []}', "expected a JSON array"),
        (None, "expected text"),
    ],
)
def test_invalid_rank_output_does_not_retry_or_publish(
    thirty_candidates: Settings,
    response: str | None,
    message: str,
) -> None:
    llm = FakeLLM([cast(str, response), "unused"])
    with pytest.raises(ValueError, match=message) as raised:
        rank_source(
            thirty_candidates, 1, campaign_id=None, top=3, budget=Budget(1, 1), llm=llm
        )
    assert "private-completion" not in str(raised.value)
    assert len(llm.prompts) == 1
    directory = thirty_candidates.data_dir / "sources/1"
    assert not (directory / "rank.json").exists()
    assert not list(directory.glob(".rank-*.json"))


def test_reported_truncation_is_billed_once_and_a_later_run_can_recover(
    thirty_candidates: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = thirty_candidates.model_copy(
        update={"anthropic_api_key": SecretStr("offline-fake")}
    )
    requests: list[int] = []

    def send(request: Request, *, timeout: int) -> BytesIO:
        assert request.data is not None
        payload = json.loads(request.data)
        assert payload["thinking"] == {"type": "between_tools"}
        requests.append(payload["max_tokens"])
        truncated = len(requests) == 1
        text = (
            '[{"private-completion"'
            if truncated
            else json.dumps([grade(i) for i in range(1, 31)])
        )
        return BytesIO(
            json.dumps(
                {
                    "content": [{"type": "text", "text": text}],
                    "stop_reason": "max_tokens" if truncated else "end_turn",
                    "usage": {
                        "input_tokens": 300,
                        "output_tokens": 4096 if truncated else 610,
                    },
                }
            ).encode()
        )

    monkeypatch.setattr("montaj.claude.urlopen", send)
    with pytest.raises(BudgetExceeded):
        rank_source(settings, 1, campaign_id=None, top=3, budget=Budget(0.03, 1))
    assert requests == []
    with pytest.raises(ExternalCallError, match="truncated.*4096") as raised:
        rank_source(settings, 1, campaign_id=None, top=3, budget=Budget(1, 1))
    assert "private-completion" not in str(raised.value)
    assert requests == [4096]
    with connect(settings.db_path) as db:
        rows = db.execute(
            "SELECT input_tokens, output_tokens, estimated_cost_usd FROM api_calls"
        ).fetchall()
    assert len(rows) == 1 and tuple(rows[0]) == (300, 4096, pytest.approx(0.04156))
    result, reused = rank_source(
        settings, 1, campaign_id=None, top=3, budget=Budget(1, 1)
    )
    assert not reused and result.selected_ids == [1, 2, 3]
    assert requests == [4096, 4096]


def test_invalid_paid_cache_preserves_prior_selection_without_spending(
    thirty_candidates: Settings,
) -> None:
    llm = FakeLLM([json.dumps([grade(i) for i in range(1, 31)])])
    rank_source(
        thirty_candidates, 1, campaign_id=None, top=3, budget=Budget(1, 1), llm=llm
    )
    directory = thirty_candidates.data_dir / "sources/1"
    previous = (directory / "rank.json").read_bytes()
    (cache,) = directory.glob(".rank-*.json")
    cache.write_text("[]", encoding="utf-8")
    with pytest.raises(
        ValueError, match="Saved rank grade cache is invalid.*missing candidate IDs"
    ):
        rank_source(
            thirty_candidates, 1, campaign_id=None, top=4, budget=Budget(0, 0), llm=llm
        )
    assert len(llm.prompts) == 1
    assert (directory / "rank.json").read_bytes() == previous


@pytest.mark.parametrize("damage", ["missing", "duplicate", "selection"])
def test_damaged_current_ranking_is_recovered_from_grades(
    thirty_candidates: Settings, damage: str
) -> None:
    answers = [grade(i, {5: 9, 19: 10, 28: 9}.get(i, 5)) for i in range(1, 31)]
    llm = FakeLLM([json.dumps(answers)])
    original, _ = rank_source(
        thirty_candidates, 1, campaign_id=None, top=3, budget=Budget(1, 1), llm=llm
    )
    path = thirty_candidates.data_dir / "sources/1/rank.json"
    body = json.loads(path.read_bytes())
    if damage == "missing":
        body["clips"].pop(0)
    elif damage == "duplicate":
        body["clips"][-1] = body["clips"][0]
    else:
        body["clips"][0]["selected"] = False
        body["clips"][3]["selected"] = True
    path.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(ValueError, match="Saved ranking is invalid"):
        selected_clips(thirty_candidates, 1, path.parent)
    recovered, reused = rank_source(
        thirty_candidates, 1, campaign_id=None, top=3, budget=Budget(0, 0), llm=llm
    )
    assert not reused
    assert recovered == original
    assert recovered.selected_ids == [19, 5, 28]
    assert len(llm.prompts) == 1


def test_damaged_selection_without_grades_requires_explicit_recovery(
    thirty_candidates: Settings,
) -> None:
    llm = FakeLLM([json.dumps([grade(i) for i in range(1, 31)])])
    rank_source(
        thirty_candidates, 1, campaign_id=None, top=3, budget=Budget(1, 1), llm=llm
    )
    directory = thirty_candidates.data_dir / "sources/1"
    (grades,) = directory.glob(".rank-*.json")
    grades.rename(directory / "saved-grades.json")
    path = directory / "rank.json"
    path.write_text("{", encoding="utf-8")
    with pytest.raises(ValueError, match="cached grades are unavailable"):
        rank_source(
            thirty_candidates, 1, campaign_id=None, top=3, budget=Budget(1, 1), llm=llm
        )
    assert path.read_bytes() == b"{"
    assert len(llm.prompts) == 1


@pytest.mark.parametrize(
    ("reason", "body", "error", "message"),
    [
        ("max_tokens", "[]", ExternalCallError, "truncated"),
        ("model_context_window_exceeded", "[]", ExternalCallError, "context limit"),
        ("refusal", "[]", ExternalCallError, "declined"),
        ("stop_sequence", "[]", ExternalCallError, "complete response"),
        (None, "[]", ExternalCallError, "complete response"),
        ("end_turn", "[]", ValueError, "missing candidate IDs"),
        (
            "end_turn",
            '{"private-completion":',
            ValueError,
            "invalid or incomplete JSON",
        ),
    ],
)
def test_rejected_rank_completion_counts_as_failed_at_reported_usage(
    thirty_candidates: Settings,
    monkeypatch: pytest.MonkeyPatch,
    reason: str | None,
    body: str,
    error: type[Exception],
    message: str,
) -> None:
    settings = thirty_candidates.model_copy(
        update={"anthropic_api_key": SecretStr("offline-fake")}
    )
    requests = []

    def send(request: Request, *, timeout: int) -> BytesIO:
        requests.append(request.data)
        return BytesIO(
            json.dumps(
                {
                    "content": [{"type": "text", "text": body}],
                    "stop_reason": reason,
                    "usage": {"input_tokens": 1500, "output_tokens": 610},
                }
            ).encode()
        )

    monkeypatch.setattr("montaj.claude.urlopen", send)
    with pytest.raises(error, match=message) as raised:
        rank_source(settings, 1, campaign_id=None, top=3, budget=Budget(1, 1))
    assert "private-completion" not in str(raised.value)
    assert len(requests) == 1
    assert not (settings.data_dir / "sources/1/rank.json").exists()
    with connect(settings.db_path) as db:
        row = db.execute(
            "SELECT input_tokens, output_tokens, outcome FROM api_calls"
        ).fetchone()
        totals = cost_totals(db, 1)
    assert tuple(row) == (1500, 610, "failure")
    assert totals.approved_clips == 0
    assert totals.total == pytest.approx(0.0091)
