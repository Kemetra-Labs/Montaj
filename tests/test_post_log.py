from datetime import UTC, date, datetime

import pytest
from typer.testing import CliRunner

from montaj.check import check_source
from montaj.cli import app
from montaj.config import Settings
from montaj.db import connect, fetch, insert, save_clip
from montaj.export import export_inputs
from montaj.models import (
    Account,
    Campaign,
    CampaignRules,
    Clip,
    ClipStage,
    ClipStatus,
    Niche,
    Platform,
    Source,
)
from montaj.pipeline import next_stage, stage_input_hash
from montaj.post_log import format_post_log, log_post, logged_posts, parse_posted_at
from montaj.rank import RankedClip, RankSet

_URL = "https://example.test/post"


def _account(settings: Settings, handle: str = "play") -> Account:
    with connect(settings.db_path) as db:
        return insert(
            db,
            Account(platform=Platform.YOUTUBE, handle=handle, niche=Niche.GAMES),
        )


def _export(settings: Settings, clip: Clip, account: Account) -> None:
    clip.status = ClipStatus.EXPORTED
    clip.account_id = account.id
    with connect(settings.db_path) as db:
        save_clip(db, clip)


def test_logs_an_exported_clip_and_marks_it_posted(
    prepared_source: Settings, ranked_clip: Clip
) -> None:
    account = _account(prepared_source)
    assert account.id is not None
    _export(prepared_source, ranked_clip, account)
    (prepared_source.data_dir / "sources/1/transcript.json").write_text(
        '{"text":"ship","language":"en","words":[]}', encoding="utf-8"
    )
    when = datetime(2026, 11, 22, 15, 4, tzinfo=UTC)
    post = log_post(prepared_source, 1, account.id, _URL, posted_at=when)
    assert post.platform == Platform.YOUTUBE
    assert post.url == _URL
    assert post.posted_at == when
    with connect(prepared_source.db_path) as db:
        stored = fetch(db, Clip, 1)
        source = fetch(db, Source, 1)
    assert stored is not None and source is not None
    assert stored.status == ClipStatus.POSTED
    assert next_stage(stored) is None
    inputs_hash = stored.input_hashes[ClipStage.POST_LOG]

    assert inputs_hash == stage_input_hash(
        stored, ClipStage.POST_LOG, export_inputs(prepared_source, source)
    )


def test_refuses_a_missing_clip_or_account(
    prepared_source: Settings, ranked_clip: Clip
) -> None:
    account = _account(prepared_source)
    assert account.id is not None
    _export(prepared_source, ranked_clip, account)
    with pytest.raises(ValueError, match="does not exist"):
        log_post(prepared_source, 9, account.id, _URL)
    with pytest.raises(ValueError, match="Account 9 does not exist"):
        log_post(prepared_source, 1, 9, _URL)


def test_refuses_a_clip_that_is_not_exported(
    prepared_source: Settings, ranked_clip: Clip
) -> None:
    account = _account(prepared_source)
    assert account.id is not None
    with pytest.raises(ValueError, match="not exported"):
        log_post(prepared_source, 1, account.id, _URL)
    assert logged_posts(prepared_source) == []


def test_refuses_a_second_post_for_the_same_account(
    prepared_source: Settings, ranked_clip: Clip
) -> None:
    account = _account(prepared_source)
    other = _account(prepared_source, "other")
    assert account.id is not None and other.id is not None
    _export(prepared_source, ranked_clip, account)
    log_post(prepared_source, 1, account.id, _URL)
    with pytest.raises(ValueError, match="already posted to this account"):
        log_post(prepared_source, 1, account.id, "https://example.test/again")
    with pytest.raises(ValueError, match="not exported"):
        log_post(prepared_source, 1, other.id, "https://example.test/other")
    assert len(logged_posts(prepared_source)) == 1
    with connect(prepared_source.db_path) as db:
        stored = fetch(db, Clip, 1)
    assert stored is not None and stored.status == ClipStatus.POSTED


def test_refuses_a_url_that_is_not_http(
    prepared_source: Settings, ranked_clip: Clip
) -> None:
    account = _account(prepared_source)
    assert account.id is not None
    _export(prepared_source, ranked_clip, account)
    with pytest.raises(ValueError, match="http or https"):
        log_post(prepared_source, 1, account.id, "not a url")
    assert logged_posts(prepared_source) == []


def test_list_counts_posts_per_account_per_day(
    prepared_source: Settings, ranked_clip: Clip
) -> None:
    first = _account(prepared_source, "play")
    second = _account(prepared_source, "other")
    assert first.id is not None and second.id is not None
    _export(prepared_source, ranked_clip, first)
    with connect(prepared_source.db_path) as db:
        later = insert(
            db,
            Clip(source_id=1, start_s=1, end_s=2, status=ClipStatus.EXPORTED),
        )
        earlier = insert(
            db,
            Clip(
                source_id=1,
                start_s=1,
                end_s=2,
                status=ClipStatus.EXPORTED,
                account_id=first.id,
            ),
        )
    assert later.id is not None and earlier.id is not None
    log_post(
        prepared_source,
        earlier.id,
        first.id,
        "https://example.test/old",
        posted_at=datetime(2026, 11, 21, 9, tzinfo=UTC),
    )
    log_post(
        prepared_source,
        1,
        first.id,
        "https://example.test/new",
        posted_at=datetime(2026, 11, 22, 15, tzinfo=UTC),
    )
    later.account_id = second.id
    _export(prepared_source, later, second)
    log_post(
        prepared_source,
        later.id,
        second.id,
        "https://example.test/other",
        posted_at=datetime(2026, 11, 22, 16, tzinfo=UTC),
    )
    shown = format_post_log(logged_posts(prepared_source))
    assert "2026-11-21  account 1 @play  1 post" in shown
    assert "2026-11-22  account 1 @play  1 post" in shown
    assert "2026-11-22  account 2 @other  1 post" in shown
    assert "https://example.test/new" in shown
    only_today = format_post_log(logged_posts(prepared_source, day=date(2026, 11, 22)))
    assert "2026-11-21" not in only_today
    assert "2026-11-22  account 1 @play  1 post" in only_today
    play_only = logged_posts(prepared_source, account_id=first.id)
    assert {row.account_id for row in play_only} == {first.id}


def test_logged_post_counts_toward_the_daily_cap(
    prepared_source: Settings, ranked_clip: Clip
) -> None:
    account = _account(prepared_source)
    assert account.id is not None
    _export(prepared_source, ranked_clip, account)
    log_post(prepared_source, 1, account.id, _URL)
    directory = prepared_source.data_dir / "sources/1"
    (directory / "transcript.json").write_text(
        '{"text":"hello","language":"en","words":[{"text":"hello","start":0.5,"end":1}]}',
        encoding="utf-8",
    )
    rules = CampaignRules(min_seconds=1, max_seconds=60, max_posts_per_day=1)
    with connect(prepared_source.db_path) as db:
        campaign = insert(
            db,
            Campaign(
                name="Daily",
                platform=Platform.YOUTUBE,
                rules_json=rules.model_dump_json(),
            ),
        )
        second = insert(
            db,
            Clip(
                source_id=1,
                start_s=0.5,
                end_s=2.5,
                status=ClipStatus.DRAFTED,
                hook_a="One hook here",
                hook_b="Another hook here",
                hook_text="One hook here",
                chosen_hook="a",
                caption="Hello there",
            ),
        )
        db.execute("UPDATE sources SET campaign_id = ? WHERE id = 1", (campaign.id,))
    assert second.id is not None
    rank = RankSet(
        input_hash="fixture",
        clips=[
            RankedClip(clip_id=1, score=8, reason="Fixture", selected=True),
            RankedClip(clip_id=second.id, score=7, reason="Next", selected=True),
        ],
    )
    (directory / "rank.json").write_text(rank.model_dump_json(), encoding="utf-8")
    outcomes = check_source(prepared_source, 1, account_id=account.id)
    assert len(outcomes) == 1
    assert outcomes[0][0].id == second.id
    assert "Daily post cap" in outcomes[0][1].reasons[0]
    with connect(prepared_source.db_path) as db:
        stored = fetch(db, Clip, 1)
    assert stored is not None and stored.status == ClipStatus.POSTED


def test_post_log_command_records_and_lists(
    prepared_source: Settings, ranked_clip: Clip
) -> None:
    account = _account(prepared_source)
    assert account.id is not None
    _export(prepared_source, ranked_clip, account)
    recorded = CliRunner().invoke(
        app,
        [
            "post-log",
            "1",
            "--account",
            str(account.id),
            "--url",
            _URL,
            "--at",
            datetime.now(UTC).isoformat(),
        ],
    )
    assert recorded.exit_code == 0
    assert "Logged post 1 for clip 1." in recorded.output
    listed = CliRunner().invoke(app, ["post-log", "list", "--today"])
    assert listed.exit_code == 0
    assert "https://example.test/post" in listed.output
    shown = CliRunner().invoke(app, ["post-log", "list"])
    assert shown.exit_code == 0
    assert "account 1 @play  1 post" in shown.output
    assert "https://example.test/post" in shown.output
    again = CliRunner().invoke(
        app,
        ["post-log", "1", "--account", str(account.id), "--url", _URL],
    )
    assert again.exit_code == 1
    assert "already posted" in again.output


def test_parse_posted_at_accepts_a_day_and_rejects_junk() -> None:
    assert parse_posted_at("2026-11-22") == datetime(2026, 11, 22, tzinfo=UTC)
    with pytest.raises(ValueError, match="ISO timestamp"):
        parse_posted_at("tomorrow")
