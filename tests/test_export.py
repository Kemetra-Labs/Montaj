import csv
import shutil
from datetime import UTC, date, datetime

from typer.testing import CliRunner

from montaj.cli import app
from montaj.config import Settings
from montaj.db import connect, fetch, insert, save_clip, save_source
from montaj.export import export_account, export_inputs
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
    Source,
    utc_now,
)
from montaj.pipeline import next_stage, stage_input_hash

_DAY = date(2026, 11, 22)
_WHEN = datetime(2026, 11, 22, 15, 4, tzinfo=UTC)


def _account(settings: Settings, handle: str = "Play Channel") -> Account:
    with connect(settings.db_path) as db:
        return insert(
            db,
            Account(platform=Platform.YOUTUBE, handle=handle, niche=Niche.GAMES),
        )


def _ready(
    settings: Settings,
    clip: Clip,
    *,
    result: CheckResult = CheckResult.PASS,
    reason: str | None = None,
    allowed: bool = True,
    basis: str | None = "creator written permission",
    status: ClipStatus = ClipStatus.APPROVED,
    payload: bytes = b"approved-video",
    final_path: str | None = None,
    hook: str = "Why did the ship jump?",
    caption: str = "The ship left early.",
) -> None:
    directory = settings.data_dir / "sources" / str(clip.source_id)
    (directory / "transcript.json").write_text(
        '{"text":"ship fast","language":"en","words":['
        '{"text":"ship","start":0.4,"end":1.0},'
        '{"text":"fast","start":1.2,"end":2.0}]}',
        encoding="utf-8",
    )
    if final_path is None:
        target = settings.data_dir / "clips" / str(clip.id) / "final.mp4"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    with connect(settings.db_path) as db:
        source = fetch(db, Source, clip.source_id)
        assert source is not None
        source.source_allowed = allowed
        source.allowed_basis = basis
        save_source(db, source)
        clip.hook_a = hook
        clip.hook_a_style = "question"
        clip.hook_b = "The ship jumped early"
        clip.hook_b_style = "bold_claim"
        clip.hook_text = hook
        clip.hook_style = "question"
        clip.chosen_hook = "a"
        clip.caption = caption
        clip.hashtags = ["#ship", "#games"]
        clip.final_path = final_path or f"clips/{clip.id}/final.mp4"
        clip.status = status
        clip.check_result = result
        clip.decision_reason = reason
        clip.reviewed_at = utc_now()
        clip.changed_since_review = False
        clip.raw_path = f"clips/{clip.id}/raw.mp4"
        inputs = export_inputs(settings, source)
        for stage in (ClipStage.CUT, ClipStage.CAPTIONS, ClipStage.DRAFT):
            clip.input_hashes[stage] = stage_input_hash(clip, stage, inputs)
        clip.check_input_hash = stage_input_hash(clip, ClipStage.CHECK, inputs)
        clip.reviewed_input_hash = stage_input_hash(clip, ClipStage.REVIEW, inputs)
        save_clip(db, clip)


def _export(settings: Settings, account_id: int) -> tuple[int, int, object, list[str]]:
    notes: list[str] = []
    exported, refused, folder = export_account(
        settings, account_id, today=_DAY, now=_WHEN, writer=notes.append
    )
    return exported, refused, folder, notes


def test_source_export_leaves_other_approved_clips_waiting(
    prepared_source: Settings, ranked_clip: Clip
) -> None:
    account = _account(prepared_source)
    root = prepared_source.data_dir / "sources"
    shutil.copytree(root / "1", root / "2")
    with connect(prepared_source.db_path) as db:
        insert(db, Source(id=2, path="other.mp4", title="Other", niche=Niche.GAMES))
        other = insert(
            db,
            Clip(id=2, source_id=2, start_s=0.5, end_s=2.5, status=ClipStatus.RANKED),
        )
    _ready(prepared_source, ranked_clip)
    _ready(prepared_source, other)
    assert account.id is not None
    exported, refused, folder = export_account(
        prepared_source, account.id, source_id=1, today=_DAY, now=_WHEN
    )
    assert (exported, refused) == (1, 0)
    assert folder is not None and folder.name == "source-1"
    with connect(prepared_source.db_path) as db:
        remaining = fetch(db, Clip, 2)
    assert remaining is not None and remaining.status == ClipStatus.APPROVED


def test_exports_text_manifest_and_marks_the_clip(
    prepared_source: Settings, ranked_clip: Clip
) -> None:
    account = _account(prepared_source)
    with connect(prepared_source.db_path) as db:
        campaign = insert(
            db,
            Campaign(
                name="Spring",
                platform=Platform.YOUTUBE,
                rules_json=CampaignRules(
                    min_seconds=20, max_seconds=60
                ).model_dump_json(),
            ),
        )
        db.execute("UPDATE sources SET campaign_id = ? WHERE id = 1", (campaign.id,))
    _ready(prepared_source, ranked_clip)
    assert account.id is not None
    exported, refused, folder, notes = _export(prepared_source, account.id)
    assert (exported, refused, notes) == (1, 0, [])
    assert folder == (
        prepared_source.data_dir
        / "exports"
        / "2026-11-22"
        / f"{account.id}-play-channel"
    )
    video = folder / "01_why-did-the-ship-jump.mp4"
    text = folder / "01_why-did-the-ship-jump.txt"
    assert video.read_bytes() == b"approved-video"
    assert text.read_text(encoding="utf-8") == (
        "Why did the ship jump?\n\nThe ship left early.\n\n#ship #games\n"
    )
    with (folder / "manifest.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows == [
        {
            "clip_id": "1",
            "file": "01_why-did-the-ship-jump.mp4",
            "platform": "youtube",
            "campaign": "Spring",
            "suggested_post_time": "2026-11-22T15:04:00+00:00",
        }
    ]
    with connect(prepared_source.db_path) as db:
        stored = fetch(db, Clip, 1)
        source = fetch(db, Source, 1)
    assert stored is not None and source is not None
    assert stored.status == ClipStatus.EXPORTED
    assert stored.account_id == account.id
    inputs = export_inputs(prepared_source, source)
    assert stored.input_hashes[ClipStage.EXPORT] == stage_input_hash(
        stored, ClipStage.EXPORT, inputs
    )
    assert next_stage(stored, inputs) == ClipStage.POST_LOG


def test_second_run_keeps_exported_files_and_appends(
    prepared_source: Settings, ranked_clip: Clip
) -> None:
    account = _account(prepared_source)
    assert account.id is not None
    _ready(prepared_source, ranked_clip)
    _exported, _refused, folder, _notes = _export(prepared_source, account.id)
    assert folder is not None
    original = (folder / "01_why-did-the-ship-jump.mp4").read_bytes()
    with connect(prepared_source.db_path) as db:
        second = insert(
            db,
            Clip(source_id=1, start_s=0.5, end_s=2.5, status=ClipStatus.RANKED),
        )
    _ready(
        prepared_source,
        second,
        hook="Another hook lands",
        caption="Second caption.",
        payload=b"second-video",
    )
    exported, refused, again, _notes = _export(prepared_source, account.id)
    assert (exported, refused, again) == (1, 0, folder)
    assert (folder / "01_why-did-the-ship-jump.mp4").read_bytes() == original
    assert (folder / "02_another-hook-lands.mp4").read_bytes() == b"second-video"
    with (folder / "manifest.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert [row["clip_id"] for row in rows] == ["1", "2"]
    assert rows[0]["file"] == "01_why-did-the-ship-jump.mp4"


def test_unapproved_clip_is_not_copied(
    prepared_source: Settings, ranked_clip: Clip
) -> None:
    account = _account(prepared_source)
    assert account.id is not None
    _ready(prepared_source, ranked_clip, status=ClipStatus.CHECKED)
    exported, refused, folder, notes = _export(prepared_source, account.id)
    assert (exported, refused, folder) == (0, 0, None)
    assert notes == ["No approved clips are waiting for this account."]
    assert list(prepared_source.data_dir.rglob("exports/**/*.mp4")) == []


def test_disallowed_source_is_refused(
    prepared_source: Settings, ranked_clip: Clip
) -> None:
    account = _account(prepared_source)
    assert account.id is not None
    _ready(prepared_source, ranked_clip, allowed=False)
    exported, refused, folder, notes = _export(prepared_source, account.id)
    assert (exported, refused, folder) == (0, 1, None)
    assert "not allowed" in notes[0]
    assert list((prepared_source.data_dir / "exports").rglob("*.mp4")) == []
    with connect(prepared_source.db_path) as db:
        stored = fetch(db, Clip, 1)
    assert stored is not None and stored.status == ClipStatus.APPROVED


def test_missing_basis_is_refused(prepared_source: Settings, ranked_clip: Clip) -> None:
    account = _account(prepared_source)
    assert account.id is not None
    _ready(prepared_source, ranked_clip, basis=None)
    exported, refused, folder, notes = _export(prepared_source, account.id)
    assert (exported, refused, folder) == (0, 1, None)
    assert "permission basis" in notes[0]


def test_failed_or_unchecked_clip_is_refused(
    prepared_source: Settings, ranked_clip: Clip
) -> None:
    account = _account(prepared_source)
    assert account.id is not None
    _ready(prepared_source, ranked_clip, result=CheckResult.FAIL, reason="Too long.")
    exported, refused, folder, notes = _export(prepared_source, account.id)
    assert (exported, refused, folder) == (0, 1, None)
    assert "failed" in notes[0]
    with connect(prepared_source.db_path) as db:
        stored = fetch(db, Clip, 1)
        assert stored is not None
        stored.check_result = None
        save_clip(db, stored)
    exported, refused, _folder, notes = _export(prepared_source, account.id)
    assert (exported, refused) == (0, 1)
    assert "not been checked" in notes[0]


def test_stale_check_is_refused(prepared_source: Settings, ranked_clip: Clip) -> None:
    account = _account(prepared_source)
    assert account.id is not None
    _ready(prepared_source, ranked_clip)
    with connect(prepared_source.db_path) as db:
        stored = fetch(db, Clip, 1)
        assert stored is not None
        stored.check_input_hash = "stale"
        save_clip(db, stored)
    exported, refused, folder, notes = _export(prepared_source, account.id)
    assert (exported, refused, folder) == (0, 1, None)
    assert "not current" in notes[0]
    assert list((prepared_source.data_dir / "exports").rglob("*.mp4")) == []


def test_unconfirmed_warning_is_refused(
    prepared_source: Settings, ranked_clip: Clip
) -> None:
    account = _account(prepared_source)
    assert account.id is not None
    _ready(
        prepared_source,
        ranked_clip,
        result=CheckResult.WARN,
        reason="Possible medical claim or promise: cure",
    )
    exported, refused, folder, notes = _export(prepared_source, account.id)
    assert (exported, refused, folder) == (0, 1, None)
    assert "not confirmed" in notes[0]


def test_confirmed_warning_is_exported(
    prepared_source: Settings, ranked_clip: Clip
) -> None:
    account = _account(prepared_source)
    assert account.id is not None
    _ready(
        prepared_source,
        ranked_clip,
        result=CheckResult.WARN,
        reason="confirmed warning",
    )
    exported, refused, folder, _notes = _export(prepared_source, account.id)
    assert (exported, refused) == (1, 0)
    assert folder is not None and (folder / "01_why-did-the-ship-jump.mp4").is_file()
    with connect(prepared_source.db_path) as db:
        stored = fetch(db, Clip, 1)
    assert stored is not None
    assert stored.status == ClipStatus.EXPORTED
    assert stored.check_result == CheckResult.WARN
    assert stored.decision_reason == "confirmed warning"


def test_changed_approval_is_refused(
    prepared_source: Settings, ranked_clip: Clip
) -> None:
    account = _account(prepared_source)
    assert account.id is not None
    _ready(prepared_source, ranked_clip)
    with connect(prepared_source.db_path) as db:
        stored = fetch(db, Clip, 1)
        assert stored is not None
        stored.changed_since_review = True
        save_clip(db, stored)
    exported, refused, folder, notes = _export(prepared_source, account.id)
    assert (exported, refused, folder) == (0, 1, None)
    assert "approval is not current" in notes[0]


def test_parent_path_is_refused(prepared_source: Settings, ranked_clip: Clip) -> None:
    account = _account(prepared_source)
    assert account.id is not None
    outside = prepared_source.data_dir.parent / "secret.mp4"
    outside.write_bytes(b"secret-video")
    _ready(prepared_source, ranked_clip, final_path="../secret.mp4")
    exported, refused, folder, notes = _export(prepared_source, account.id)
    assert (exported, refused, folder) == (0, 1, None)
    assert "outside the data directory" in notes[0]
    assert list((prepared_source.data_dir / "exports").rglob("*")) == []
    assert outside.read_bytes() == b"secret-video"


def test_missing_hook_is_refused(prepared_source: Settings, ranked_clip: Clip) -> None:
    account = _account(prepared_source)
    assert account.id is not None
    _ready(prepared_source, ranked_clip)
    with connect(prepared_source.db_path) as db:
        stored = fetch(db, Clip, 1)
        source = fetch(db, Source, 1)
        assert stored is not None and source is not None
        stored.hook_text = None
        inputs = export_inputs(prepared_source, source)
        stored.check_input_hash = stage_input_hash(stored, ClipStage.CHECK, inputs)
        stored.reviewed_input_hash = stage_input_hash(stored, ClipStage.REVIEW, inputs)
        save_clip(db, stored)
    exported, refused, _folder, notes = _export(prepared_source, account.id)
    assert (exported, refused) == (0, 1)
    assert "chosen hook" in notes[0]


def test_other_account_is_not_this_export(
    prepared_source: Settings, ranked_clip: Clip
) -> None:
    first = _account(prepared_source, "Play Channel")
    second = _account(prepared_source, "Other Desk")
    assert first.id is not None and second.id is not None
    _ready(prepared_source, ranked_clip)
    with connect(prepared_source.db_path) as db:
        stored = fetch(db, Clip, 1)
        assert stored is not None
        stored.account_id = second.id
        save_clip(db, stored)
    exported, refused, folder, notes = _export(prepared_source, first.id)
    assert (exported, refused, folder) == (0, 0, None)
    assert notes == ["No approved clips are waiting for this account."]
    with connect(prepared_source.db_path) as db:
        stored = fetch(db, Clip, 1)
    assert stored is not None and stored.status == ClipStatus.APPROVED


def test_export_command_copies_one_clip(
    prepared_source: Settings, ranked_clip: Clip
) -> None:
    account = _account(prepared_source)
    assert account.id is not None
    _ready(prepared_source, ranked_clip)
    result = CliRunner().invoke(app, ["export", "--account", str(account.id)])
    assert result.exit_code == 0
    assert "Exported 1 clip to" in result.output
    assert list((prepared_source.data_dir / "exports").rglob("*.txt"))


def test_export_command_refuses_a_missing_account(prepared_source: Settings) -> None:
    result = CliRunner().invoke(app, ["export", "--account", "9"])
    assert result.exit_code == 1
    assert "does not exist" in result.output
