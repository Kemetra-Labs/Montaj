import json

from montaj.budget import Budget
from montaj.captions import source_inputs
from montaj.config import Settings
from montaj.db import connect, fetch, insert, save_clip
from montaj.draft import draft_source
from montaj.models import (
    Account,
    Campaign,
    CampaignRules,
    Clip,
    ClipStage,
    Niche,
    Platform,
)
from montaj.pipeline import input_hash, stage_input_hash
from montaj.transcribe import Transcript, Word
from tests.fakes import FakeLLM


def _captioned(settings: Settings) -> None:
    directory = settings.data_dir / "sources/1"
    words = [
        Word(text="ship", start=0.5, end=1.0),
        Word(text="fast", start=1.2, end=2.0),
    ]
    transcript = Transcript(text="ship fast", language="en", words=words)
    (directory / "transcript.json").write_text(
        transcript.model_dump_json(), encoding="utf-8"
    )
    rules = CampaignRules(
        min_seconds=1,
        max_seconds=60,
        required_hashtags=["#Clip"],
        banned_words=["forbidden"],
    )
    with connect(settings.db_path) as db:
        campaign = insert(
            db,
            Campaign(
                name="Test",
                platform=Platform.YOUTUBE,
                rules_json=rules.model_dump_json(),
            ),
        )
        insert(
            db,
            Account(
                platform=Platform.YOUTUBE,
                handle="play",
                niche=Niche.GAMES,
                style_notes="Mention the patch.",
            ),
        )
        assert campaign.id is not None
        db.execute("UPDATE sources SET campaign_id = ? WHERE id = 1", (campaign.id,))
        clip = fetch(db, Clip, 1)
        assert clip is not None
        inputs = source_inputs(
            settings, directory, rules_hash=input_hash(rules.model_dump(mode="json"))
        )
        clip.final_path = "clips/1/final.mp4"
        clip.moment_type = "story"
        clip.input_hashes[ClipStage.CAPTIONS] = stage_input_hash(
            clip, ClipStage.CAPTIONS, inputs
        )
        save_clip(db, clip)


def _valid_draft() -> str:
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


def test_draft_parses_hooks_and_sends_style_notes(
    ranked_clip: Clip, prepared_source: Settings
) -> None:
    del ranked_clip
    _captioned(prepared_source)
    llm = FakeLLM([_valid_draft()])
    drafted, reused = draft_source(
        prepared_source, 1, clip_id=None, budget=Budget(1, 1), llm=llm
    )
    assert (drafted, reused) == (1, 0)
    assert "Mention the patch." in llm.prompts[0]
    assert "play or decision" in llm.prompts[0]
    assert "#Clip" in llm.prompts[0]
    assert "forbidden" in llm.prompts[0]
    with connect(prepared_source.db_path) as db:
        clip = fetch(db, Clip, 1)
    assert clip is not None
    assert clip.hook_a == "Why did the ship jump?"
    assert clip.hook_b_style == "bold_claim"
    assert clip.chosen_hook is None
    assert clip.hashtags == ["#Clip"]
    assert clip.status.value == "drafted"

    again, reused_again = draft_source(
        prepared_source, 1, clip_id=None, budget=Budget(1, 1), llm=FakeLLM([])
    )
    assert (again, reused_again) == (0, 1)


def test_long_hook_is_retried_then_accepted(
    ranked_clip: Clip, prepared_source: Settings
) -> None:
    del ranked_clip
    _captioned(prepared_source)
    long_hook = " ".join(["word"] * 13)
    first = json.dumps(
        {
            "hooks": [
                {"text": long_hook, "style": "question"},
                {"text": "Short hook", "style": "story"},
            ],
            "caption": "Caption",
            "hashtags": ["#Clip"],
        }
    )
    llm = FakeLLM([first, _valid_draft()])
    drafted, _reused = draft_source(
        prepared_source, 1, clip_id=1, budget=Budget(1, 1), llm=llm
    )
    assert drafted == 1
    assert len(llm.prompts) == 2
