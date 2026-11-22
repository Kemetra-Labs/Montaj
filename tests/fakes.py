"""Predictable stand-ins for the external pipeline services."""

import json
import shutil
from collections import deque
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


class FakeDownloader:
    def __init__(self, video: Path) -> None:
        self.video = video
        self.urls: list[str] = []

    def download(self, url: str, destination: Path) -> None:
        self.urls.append(url)
        shutil.copyfile(self.video, destination)


class FakeTranscriber:
    def __init__(self, transcript: Path) -> None:
        self.transcript = transcript
        self.audio_inputs: list[Path] = []

    def transcribe(self, audio_path: Path) -> dict[str, Any]:
        self.audio_inputs.append(audio_path)
        value: dict[str, Any] = json.loads(self.transcript.read_text(encoding="utf-8"))
        return value


class FakeLLM:
    def __init__(self, responses: Iterable[str]) -> None:
        self.responses = deque(responses)
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.responses.popleft()


class FakeClock:
    def __init__(self) -> None:
        self.current = datetime(2025, 1, 1, tzinfo=UTC)
        self.delays: list[float] = []

    def now(self) -> datetime:
        return self.current

    def sleep(self, seconds: float) -> None:
        self.delays.append(seconds)
        self.current += timedelta(seconds=seconds)


class FakeTransport[T]:
    def __init__(self, outcomes: Iterable[T | Exception]) -> None:
        self.outcomes = deque(outcomes)
        self.requests: list[object] = []

    def send(self, request: object) -> T:
        self.requests.append(request)
        outcome = self.outcomes.popleft()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
