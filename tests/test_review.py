import pytest
from typer.testing import CliRunner

from montaj.cli import app
from montaj.config import Settings
from montaj.db import connect, fetch, insert, save_clip, save_source
from montaj.models import (
    Account,
    CheckResult,
    Clip,
    ClipStatus,
    Niche,
    Platform,
    Source,
)
from montaj.review import review_source


def _prepare(
    settings: Settings,
    clip: Clip,
    *,
    result: CheckResult,
    reason: str | None = None,
) -> None:
    directory = settings.data_dir / "sources/1"
    (directory / "transcript.json").write_text(
        '{"text":"ship fast","language":"en","words":['
        '{"text":"ship","start":0.4,"end":1.0},'
        '{"text":"fast","start":1.2,"end":2.0}]}',
        encoding="utf-8",
    )
    with connect(settings.db_path) as db:
        source = fetch(db, Source, 1)
        assert source is not None
        source.allowed_basis = "creator written permission"
        save_source(db, source)
        clip.hook_a = "Why did the ship jump?"
        clip.hook_a_style = "question"
        clip.hook_b = "The ship jumped early"
        clip.hook_b_style = "bold_claim"
        clip.caption = "The ship left early."
        clip.final_path = "clips/1/final.mp4"
        clip.status = ClipStatus.CHECKED
        clip.check_result = result
        clip.decision_reason = reason
        save_clip(db, clip)


def _reader(lines: list[str]) -> tuple[list[str], object]:
    pending = iter(lines)
    prompts: list[str] = []

    def reader(prompt: str) -> str:
        prompts.append(prompt)
        return next(pending)

    return prompts, reader


def test_review_shows_path_basis_and_hooks(
    prepared_source: Settings, ranked_clip: Clip
) -> None:
    _prepare(prepared_source, ranked_clip, result=CheckResult.PASS)
    lines: list[str] = []
    _, reader = _reader(["q"])
    review_source(prepared_source, 1, reader=reader, writer=lines.append)
    shown = "\n".join(lines)
    assert "clips/1/final.mp4" in shown
    assert "creator written permission" in shown
    assert "Why did the ship jump?" in shown
    assert "ship fast" in shown
    assert "http://127.0.0.1:8765/media/1" in shown


def test_failed_check_cannot_be_approved(
    prepared_source: Settings, ranked_clip: Clip
) -> None:
    _prepare(
        prepared_source,
        ranked_clip,
        result=CheckResult.FAIL,
        reason="Length is outside the campaign.",
    )
    _, reader = _reader(["a", "q"])
    notes: list[str] = []
    assert review_source(prepared_source, 1, reader=reader, writer=notes.append) == 0
    assert "cannot be approved" in "\n".join(notes)
    with connect(prepared_source.db_path) as db:
        stored = fetch(db, Clip, 1)
    assert stored is not None
    assert stored.status == ClipStatus.CHECKED
    assert stored.reviewed_at is None


def test_warning_needs_confirmation_and_records_the_review_time(
    prepared_source: Settings, ranked_clip: Clip
) -> None:
    _prepare(
        prepared_source,
        ranked_clip,
        result=CheckResult.WARN,
        reason="Possible medical claim or promise: cure",
    )
    _, reader = _reader(["a", "n", "a", "y"])
    notes: list[str] = []
    assert review_source(prepared_source, 1, reader=reader, writer=notes.append) == 1
    assert "not confirmed" in "\n".join(notes)
    with connect(prepared_source.db_path) as db:
        stored = fetch(db, Clip, 1)
    assert stored is not None
    assert stored.status == ClipStatus.APPROVED
    assert stored.decision_reason == "confirmed warning"
    assert stored.reviewed_at is not None
    assert stored.reviewed_input_hash


def test_reject_stores_a_one_word_reason(
    prepared_source: Settings, ranked_clip: Clip
) -> None:
    _prepare(prepared_source, ranked_clip, result=CheckResult.PASS)
    _, reader = _reader(["r", "too long", "r", "offtopic"])
    assert (
        review_source(prepared_source, 1, reader=reader, writer=lambda _line: None) == 1
    )
    with connect(prepared_source.db_path) as db:
        stored = fetch(db, Clip, 1)
    assert stored is not None
    assert stored.status == ClipStatus.REJECTED
    assert stored.decision_reason == "offtopic"
    assert stored.reviewed_at is not None


def test_edits_rerun_only_the_affected_stages(
    prepared_source: Settings, ranked_clip: Clip, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare(prepared_source, ranked_clip, result=CheckResult.PASS)
    calls: list[tuple[str, int | None]] = []

    def fake_check(
        _settings: Settings,
        _source_id: int,
        *,
        clip_id: int | None = None,
        account_id: int | None = None,
    ) -> list[object]:
        del account_id
        calls.append(("check", clip_id))
        return []

    def fake_cut(
        _settings: Settings,
        _source_id: int,
        clip_id: int | None = None,
        *,
        renderer: object = None,
    ) -> tuple[int, int]:
        del renderer
        calls.append(("cut", clip_id))
        return 1, 0

    def fake_caption(
        _settings: Settings, _source_id: int, clip_id: int | None = None
    ) -> tuple[int, int]:
        calls.append(("caption", clip_id))
        return 1, 0

    monkeypatch.setattr("montaj.review.check_source", fake_check)
    monkeypatch.setattr("montaj.review.cut_source", fake_cut)
    monkeypatch.setattr("montaj.review.caption_source", fake_caption)
    _, reader = _reader(["1", "c", "A shorter caption", "t", "0.1 -0.2", "q"])
    review_source(prepared_source, 1, reader=reader, writer=lambda _line: None)
    assert calls == [
        ("check", 1),
        ("check", 1),
        ("cut", 1),
        ("caption", 1),
        ("check", 1),
    ]
    with connect(prepared_source.db_path) as db:
        stored = fetch(db, Clip, 1)
    assert stored is not None
    assert stored.chosen_hook == "a"
    assert stored.hook_text == "Why did the ship jump?"
    assert stored.caption == "A shorter caption"
    assert stored.start_s == ranked_clip.start_s + 0.1
    assert stored.end_s == ranked_clip.end_s - 0.2


def test_account_pick_is_stored_and_rechecked(
    prepared_source: Settings, ranked_clip: Clip, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare(prepared_source, ranked_clip, result=CheckResult.PASS)
    with connect(prepared_source.db_path) as db:
        account = insert(
            db,
            Account(platform=Platform.YOUTUBE, handle="play", niche=Niche.GAMES),
        )
    assert account.id is not None
    seen: list[int | None] = []

    def fake_check(
        _settings: Settings,
        _source_id: int,
        *,
        clip_id: int | None = None,
        account_id: int | None = None,
    ) -> list[object]:
        del clip_id
        seen.append(account_id)
        return []

    monkeypatch.setattr("montaj.review.check_source", fake_check)
    _, reader = _reader(["u", str(account.id), "q"])
    review_source(prepared_source, 1, reader=reader, writer=lambda _line: None)
    assert seen == [account.id]
    with connect(prepared_source.db_path) as db:
        stored = fetch(db, Clip, 1)
    assert stored is not None and stored.account_id == account.id


def test_review_command_reads_scripted_keys(
    prepared_source: Settings, ranked_clip: Clip
) -> None:
    _prepare(prepared_source, ranked_clip, result=CheckResult.PASS)
    result = CliRunner().invoke(app, ["review", "1"], input="a\n")
    assert result.exit_code == 0
    assert "Decisions recorded: 1" in result.output
    with connect(prepared_source.db_path) as db:
        stored = fetch(db, Clip, 1)
    assert stored is not None and stored.status == ClipStatus.APPROVED
