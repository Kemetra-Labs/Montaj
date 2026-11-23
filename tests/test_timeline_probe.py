import shutil
from pathlib import Path

import pytest

from scripts.timeline_probe import Handoff, write_timeline
from scripts.timeline_probe import main as timeline_main


def clip(**overrides):
    return {
        "clip_id": 7,
        "source_id": 2,
        "approved": True,
        "path": "media/clip one.mp4",
        "media_frames": 216,
        "in_frame": 30,
        "out_frame": 90,
    } | overrides


def test_timeline_round_trip_and_relocate(tmp_path: Path) -> None:
    otio = pytest.importorskip("opentimelineio")
    original = tmp_path / "handoff"
    media = original / "media"
    media.mkdir(parents=True)
    fixture = Path(__file__).parent / "fixtures" / "sample.mp4"
    shutil.copyfile(fixture, media / "clip one.mp4")
    shutil.copyfile(fixture, media / "two.mp4")
    model = Handoff(
        fps=12,
        clips=[clip(), clip(clip_id=8, path="media/two.mp4", in_frame=0, out_frame=30)],
    )
    output = original / "review.otio"
    write_timeline(model, output)
    moved = tmp_path / "moved"
    shutil.move(original, moved)
    restored = otio.adapters.read_from_file(str(moved / output.name))
    first, second = restored.tracks[0]
    assert first.media_reference.target_url == "media/clip%20one.mp4"
    assert first.source_range.start_time.value == 30
    assert first.source_range.end_time_exclusive().value == 90
    assert first.source_range.duration.rate == 12
    assert second.range_in_parent().start_time.value == 60
    assert restored.duration().value == 90
    assert [track.kind for track in restored.tracks] == ["Video", "Audio"]
    assert restored.tracks[1][0].source_range == first.source_range
    assert restored.tracks[1][1].range_in_parent().start_time.value == 60
    assert first.metadata["montaj"] == {"clip_id": 7, "source_id": 2}
    assert (moved / "media" / "clip one.mp4").read_bytes() == fixture.read_bytes()
    with pytest.raises(FileExistsError):
        write_timeline(model, moved / output.name)


@pytest.mark.parametrize(
    "path",
    [
        "../outside.mp4",
        "C:/clips/no.mp4",
        "https://example.com/no.mp4",
        "media\\no.mp4",
    ],
)
def test_timeline_rejects_nonportable_paths(path: str) -> None:
    with pytest.raises(ValueError, match="relative"):
        Handoff(fps=12, clips=[clip(path=path)])


@pytest.mark.parametrize(
    "invalid", [{"approved": False}, {"out_frame": 217}, {"out_frame": 30}]
)
def test_timeline_rejects_unapproved_and_invalid_ranges(invalid) -> None:
    with pytest.raises(ValueError):
        Handoff(fps=12, clips=[clip(**invalid)])


def test_missing_media_fails_before_writing(tmp_path: Path) -> None:
    output = tmp_path / "review.otio"
    with pytest.raises(ValueError, match="Missing media"):
        write_timeline(Handoff(fps=12, clips=[clip()]), output)
    assert not output.exists()


def test_timeline_cli_rejects_an_output_outside_its_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manifest = workspace / "manifest.json"
    manifest.write_text(
        Handoff(fps=12, clips=[clip()]).model_dump_json(), encoding="utf-8"
    )
    monkeypatch.chdir(workspace)
    monkeypatch.setattr(
        "sys.argv", ["timeline_probe", "manifest.json", "../review.otio"]
    )
    with pytest.raises(SystemExit) as exc:
        timeline_main()
    assert exc.value.code == 1
    assert "inside the working directory" in capsys.readouterr().err
    assert not (tmp_path / "review.otio").exists()
