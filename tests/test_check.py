import pytest

from montaj.check import check_clip, check_source
from montaj.config import Settings
from montaj.db import connect, fetch, insert, save_clip
from montaj.models import (
    Account,
    Campaign,
    CampaignRules,
    CheckResult,
    Clip,
    ClipStage,
    ClipStatus,
    Niche,
    Platform,
    Post,
)
from montaj.pipeline import next_stage, stage_input_hash


def _clip(**updates: object) -> Clip:
    clip = Clip(
        id=1,
        source_id=1,
        start_s=0,
        end_s=30,
        hook_a="A clear question?",
        hook_a_style="question",
        hook_b="A clear claim",
        hook_b_style="bold_claim",
        caption="A useful point. #Clip",
        hashtags=["#Clip"],
        status=ClipStatus.DRAFTED,
    )
    return clip.model_copy(update=updates)


def _rules(**updates: object) -> CampaignRules:
    rules = CampaignRules(min_seconds=20, max_seconds=60, required_hashtags=["#Clip"])
    return rules.model_copy(update=updates)


def _judge(clip: Clip, rules: CampaignRules, **kwargs: object) -> object:
    values = {
        "transcript_text": "A useful point",
        "niche": Niche.GAMES,
        "source_allowed": True,
        "posts_today": 0,
        "already_posted": False,
        "medical": ["cure"],
    }
    values.update(kwargs)
    return check_clip(clip, rules, **values)  # type: ignore[arg-type]


def test_each_rule_has_a_pass_and_a_fail() -> None:
    passing = _judge(_clip(), _rules())
    assert passing.result == CheckResult.PASS

    short = _judge(_clip(end_s=5), _rules())
    assert short.result == CheckResult.FAIL
    assert "Length" in short.reasons[0]

    missing_tag = _judge(_clip(hashtags=[], caption="No tag"), _rules())
    assert missing_tag.result == CheckResult.FAIL
    assert missing_tag.simple_fix

    banned = _judge(
        _clip(caption="A blocked claim #Clip"), _rules(banned_words=["blocked"])
    )
    assert banned.result == CheckResult.FAIL
    assert not banned.simple_fix

    topic = _judge(
        _clip(hook_a="Unrelated promotions today"),
        _rules(banned_topics=["unrelated promotions"]),
    )
    assert topic.result == CheckResult.FAIL

    disclosed = _judge(
        _clip(caption="A useful point. #Clip #ad"), _rules(required_disclosure="#ad")
    )
    assert disclosed.result == CheckResult.PASS
    hidden = _judge(_clip(), _rules(required_disclosure="#ad"))
    assert hidden.result == CheckResult.FAIL
    assert hidden.simple_fix

    refused = _judge(_clip(), _rules(), source_allowed=False)
    assert refused.result == CheckResult.FAIL
    assert "not permitted" in refused.reasons[0]

    capped = _judge(_clip(), _rules(max_posts_per_day=1), posts_today=1)
    assert capped.result == CheckResult.FAIL
    under = _judge(_clip(), _rules(max_posts_per_day=2), posts_today=1)
    assert under.result == CheckResult.PASS

    duplicate = _judge(_clip(), _rules(), already_posted=True)
    assert duplicate.result == CheckResult.FAIL
    assert "already posted" in duplicate.reasons[0]

    medical = _judge(
        _clip(caption="This cure works. #Clip"), _rules(), niche=Niche.HEALTH
    )
    assert medical.result == CheckResult.WARN
    quiet = _judge(_clip(caption="This cure works. #Clip"), _rules())
    assert quiet.result == CheckResult.PASS
    flagged = _judge(
        _clip(caption="This cure works. #Clip"),
        _rules(flag_medical_claims=True),
    )
    assert flagged.result == CheckResult.WARN


def test_failed_check_is_stored_and_stays_visible(
    prepared_source: Settings, ranked_clip: Clip
) -> None:
    directory = prepared_source.data_dir / "sources/1"
    (directory / "transcript.json").write_text(
        '{"text":"hello","language":"en","words":[{"text":"hello","start":0.5,"end":1}]}',
        encoding="utf-8",
    )
    rules = CampaignRules(
        min_seconds=20, max_seconds=60, required_hashtags=["#Missing"]
    )
    with connect(prepared_source.db_path) as db:
        campaign = insert(
            db,
            Campaign(
                name="Test",
                platform=Platform.YOUTUBE,
                rules_json=rules.model_dump_json(),
            ),
        )
        account = insert(
            db,
            Account(platform=Platform.YOUTUBE, handle="play", niche=Niche.GAMES),
        )
        assert campaign.id is not None and account.id is not None
        db.execute("UPDATE sources SET campaign_id = ? WHERE id = 1", (campaign.id,))
        ranked_clip.hook_a = "One hook"
        ranked_clip.hook_b = "Another hook"
        ranked_clip.caption = "No required tag"
        ranked_clip.hashtags = []
        ranked_clip.end_s = 2.5
        ranked_clip.status = ClipStatus.DRAFTED
        save_clip(db, ranked_clip)
        insert(
            db,
            Post(
                clip_id=ranked_clip.id,
                account_id=account.id,
                platform=Platform.YOUTUBE,
                url="https://example.test/post",
            ),
        )
    outcomes = check_source(prepared_source, 1, account_id=account.id)
    assert outcomes[0][1].result == CheckResult.FAIL
    with connect(prepared_source.db_path) as db:
        stored = fetch(db, Clip, 1)
    assert stored is not None
    assert stored.status == ClipStatus.NEEDS_FIX
    assert stored.check_result == CheckResult.FAIL
    stored.raw_path = "clips/1/raw.mp4"
    stored.final_path = "clips/1/final.mp4"
    for stage in (ClipStage.CUT, ClipStage.CAPTIONS, ClipStage.DRAFT):
        stored.input_hashes[stage] = stage_input_hash(stored, stage)
    stored.check_input_hash = stage_input_hash(stored, ClipStage.CHECK)
    assert next_stage(stored) == ClipStage.REVIEW


def test_unknown_account_is_rejected(
    prepared_source: Settings, ranked_clip: Clip
) -> None:
    del ranked_clip
    with pytest.raises(ValueError, match="Account 9 does not exist"):
        check_source(prepared_source, 1, account_id=9)
