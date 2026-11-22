import wave
from pathlib import Path

from tests.fakes import FakeDownloader, FakeTranscriber


def test_speech_fixture_is_small_and_timed(
    media_fixture_dir: Path, tmp_path: Path
) -> None:
    video = media_fixture_dir / "sample.mp4"
    audio = media_fixture_dir / "sample.wav"
    downloader = FakeDownloader(video)
    destination = tmp_path / "video.mp4"
    downloader.download("https://example.test/video", destination)
    assert destination.read_bytes() == video.read_bytes()

    with wave.open(str(audio), "rb") as sample:
        duration = sample.getnframes() / sample.getframerate()
    transcript = FakeTranscriber(media_fixture_dir / "transcript.json").transcribe(
        audio
    )
    assert 15 <= duration <= 20
    assert " ".join(word["text"] for word in transcript["words"]) == transcript["text"]
    end = 0.0
    for word in transcript["words"]:
        assert end <= word["start"] < word["end"] <= duration
        end = word["end"]
    assert sum(path.stat().st_size for path in media_fixture_dir.iterdir()) < 2_000_000
