"""Word-timed transcription backed by Deepgram."""

import json
import sqlite3
import wave
from array import array
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated, Protocol, Self
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import typer
from pydantic import AliasChoices, BaseModel, Field, ValidationError, model_validator

from montaj.budget import Budget, BudgetExceeded, BudgetOverride
from montaj.config import Settings
from montaj.costs import Service, Usage
from montaj.db import connect, fetch, save_source
from montaj.external import CallContext, CallResult, ExternalCallError, call_external
from montaj.ingest import load_source
from montaj.media import MediaError, atomic_text, file_hash, source_lock
from montaj.models import Source
from montaj.pipeline import input_hash

# Deepgram caps audio files at 2 GB and Nova processing time at ten minutes.
# https://developers.deepgram.com/docs/pre-recorded-audio
MAX_REQUEST_BYTES = 2_000_000_000
CHUNK_SECONDS = 600


class Word(BaseModel):
    text: str = Field(
        validation_alias=AliasChoices("text", "punctuated_word", "word"), min_length=1
    )
    start: float = Field(ge=0, allow_inf_nan=False)
    end: float = Field(ge=0, allow_inf_nan=False)
    speaker: int | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def validate_time_range(self) -> Self:
        if self.end < self.start:
            raise ValueError("Word end precedes its start.")
        return self


class Transcript(BaseModel):
    text: str
    language: str
    words: list[Word]
    input_hash: str | None = None


class Transcriber(Protocol):
    def transcribe(self, audio_path: Path) -> Transcript: ...


class DeepgramTranscriber:
    def __init__(
        self, settings: Settings, source_id: int, language: str, budget: Budget
    ) -> None:
        self.settings = settings
        self.source_id = source_id
        self.language = language
        self.budget = budget

    def _request(self, path: Path, key: str, duration: float) -> CallResult[Transcript]:
        options = urlencode(
            {
                "model": self.settings.deepgram_model,
                "language": self.language,
                "smart_format": "true",
                "diarize": "true",
            }
        )
        with path.open("rb") as audio:
            request = Request(
                f"https://api.deepgram.com/v1/listen?{options}",
                data=audio,
                method="POST",
                headers={
                    "Content-Type": "audio/wav",
                    "Content-Length": str(path.stat().st_size),
                },
            )
            request.add_unredirected_header("Authorization", f"Token {key}")
            with urlopen(request, timeout=615) as response:
                payload = json.load(response)
        channel = payload["results"]["channels"][0]
        best = channel["alternatives"][0]
        transcript = Transcript.model_validate(
            {
                "text": best["transcript"],
                "language": channel.get("detected_language") or self.language,
                "words": best["words"],
            }
        )
        seconds = payload.get("metadata", {}).get("duration", duration)
        return CallResult(transcript, Usage(seconds=seconds))

    def transcribe(self, audio_path: Path) -> Transcript:
        key = self.settings.deepgram_api_key
        if key is None:
            raise ValueError("Set DEEPGRAM_API_KEY before requesting a new transcript.")
        duration = audio_duration(audio_path)
        model = self.settings.deepgram_model
        billing_model = f"{model}-multilingual" if self.language == "multi" else model
        context = CallContext(
            self.settings.db_path,
            self.source_id,
            "transcribe",
            Service.DEEPGRAM,
            billing_model,
            self.budget,
        )
        if duration > CHUNK_SECONDS:
            context = replace(context, nonretryable_http=(504,))
        return call_external(
            context,
            Usage(seconds=duration),
            lambda: self._request(audio_path, key.get_secret_value(), duration),
            policy=self.settings.retry,
        )


class SpeechError(ValueError):
    """The transcript cannot be sent to a paid language-model stage."""

    def __init__(self, message: str, *, code: int) -> None:
        super().__init__(message)
        self.code = code


def languages_match(requested: str, detected: str) -> bool:
    """Treat a language family as a match. `multi` accepts whatever was detected."""
    if requested.casefold() == "multi":
        return True
    requested_family = requested.casefold().split("-", 1)[0]
    detected_family = detected.casefold().split("-", 1)[0]
    return requested_family == detected_family


def assess_speech(transcript: Transcript, duration: float) -> str | None:
    """Return a plain reason when the transcript is too thin to spend more money on."""
    if not transcript.text.strip() or not transcript.words:
        return "The transcript is empty."
    if duration >= 30:
        words_per_minute = len(transcript.words) / (duration / 60)
        if words_per_minute < 8:
            return (
                f"The transcript has {words_per_minute:.1f} words per minute; "
                "that is too little speech."
            )
    confidences = [
        word.confidence for word in transcript.words if word.confidence is not None
    ]
    if confidences and sum(confidences) / len(confidences) < 0.45:
        average = sum(confidences) / len(confidences)
        return f"Transcript confidence {average:.2f} is too low."
    return None


def require_usable_speech(source: Source) -> None:
    """Stop candidates, rank, and draft before they call Claude."""
    if source.speech_status == "language_mismatch":
        raise SpeechError(
            f"Source {source.id} was transcribed as a different language. "
            "Rerun transcribe with --force-language to continue.",
            code=7,
        )
    if source.speech_status == "no_usable_speech":
        raise SpeechError(f"Source {source.id} has no usable speech.", code=6)
    if source.speech_status == "failed":
        raise SpeechError(
            source.failure_note or f"Source {source.id} failed before transcription.",
            code=4,
        )


def _record_speech(
    settings: Settings,
    source_id: int,
    transcript: Transcript,
    requested: str,
    *,
    force_language: bool,
) -> None:
    with connect(settings.db_path) as db:
        source = fetch(db, Source, source_id)
        if source is None:
            raise SpeechError(f"Source {source_id} does not exist.", code=1)
        source.requested_language = requested
        mismatch = not languages_match(requested, transcript.language)
        thin = assess_speech(
            transcript,
            audio_duration(
                settings.data_dir / "sources" / str(source_id) / "audio.wav"
            ),
        )
        error: SpeechError | None = None
        if mismatch and not force_language:
            source.speech_status = "language_mismatch"
            source.failure_note = (
                f"Requested {requested}, detected {transcript.language}."
            )
            error = SpeechError(
                f"{source.failure_note} No further paid stages will run.",
                code=7,
            )
        elif thin:
            source.speech_status = "no_usable_speech"
            source.failure_note = thin
            error = SpeechError(f"{thin} Candidates and rank will not run.", code=6)
        else:
            source.speech_status = "ok"
            source.failure_note = None
        save_source(db, source)
    if error is not None:
        raise error


def audio_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as audio:
        return audio.getnframes() / audio.getframerate()


def _quiet_boundary(audio: wave.Wave_read, target: int, start: int) -> int:
    rate = audio.getframerate()
    step = rate // 4
    first = max(start + step, target - 4 * rate)
    audio.setpos(first)
    samples = array("h", audio.readframes(8 * rate))
    energies = [
        sum(abs(value) for value in samples[i : i + step : 8])
        for i in range(0, len(samples) - step + 1, step)
    ]
    if not energies:
        return target
    return (
        first + min(range(len(energies)), key=energies.__getitem__) * step + step // 2
    )


def _chunks(source: Path, temporary: Path) -> Iterator[tuple[Path, float]]:
    with wave.open(str(source), "rb") as audio:
        rate = audio.getframerate()
        total = audio.getnframes()
        start = 0
        while start < total:
            end = min(total, start + CHUNK_SECONDS * rate)
            if end + 4 * rate < total and end - start > 8 * rate:
                end = _quiet_boundary(audio, end, start)
            audio.setpos(start)
            path = temporary / "part.wav"
            with wave.open(str(path), "wb") as part:
                part.setparams(audio.getparams())
                part.writeframes(audio.readframes(end - start))
            yield path, start / rate
            start = end


def _read_cached(path: Path) -> Transcript | None:
    try:
        return Transcript.model_validate_json(path.read_bytes())
    except (OSError, ValidationError):
        return None


def _join(parts: list[tuple[float, Transcript]], language: str) -> Transcript:
    words: list[Word] = []
    for offset, part in parts:
        for index, word in enumerate(part.words):
            shifted = word.model_copy(
                update={"start": word.start + offset, "end": word.end + offset}
            )
            if (
                index == 0
                and words
                and shifted.text.casefold() == words[-1].text.casefold()
                and abs(shifted.start - words[-1].start) < 0.1
            ):
                continue
            words.append(shifted)
    return Transcript(
        text=" ".join(part.text for _, part in parts),
        language=parts[0][1].language if parts else language,
        words=words,
    )


def _transcribe_chunks(
    path: Path, backend: Transcriber, directory: Path, key: str, language: str
) -> Transcript:
    cache = directory / ".transcribe-parts" / key
    cache.mkdir(parents=True, exist_ok=True)
    parts = []
    with TemporaryDirectory(dir=directory) as temporary:
        for index, (chunk, offset) in enumerate(_chunks(path, Path(temporary))):
            saved = cache / f"{index}.json"
            transcript = _read_cached(saved)
            if transcript is None:
                transcript = backend.transcribe(chunk)
                atomic_text(saved, transcript.model_dump_json())
            parts.append((offset, transcript))
    return _join(parts, language)


def transcribe_source(
    settings: Settings,
    source_id: int,
    *,
    language: str,
    budget: Budget,
    backend: Transcriber | None = None,
    force_language: bool = False,
    wait: float = 0,
) -> tuple[Transcript, bool]:
    _, _, directory = load_source(settings, source_id)
    audio = directory / "audio.wav"
    output = directory / "transcript.json"
    split_marker = directory / "transcript.split"
    client = backend or DeepgramTranscriber(settings, source_id, language, budget)
    with source_lock(directory, command="transcribe", wait=wait):
        # Nova-3 supports en and Egyptian Arabic ar-EG with word timestamps.
        # https://developers.deepgram.com/changelog/2026/1/27
        key = input_hash(
            file_hash(audio),
            settings.deepgram_model,
            language,
            "diarize-smart-format-v1",
        )
        cached = _read_cached(output)
        if cached is not None and cached.input_hash == key:
            transcript, reused = cached, True
        else:
            if audio.stat().st_size >= MAX_REQUEST_BYTES or (
                split_marker.is_file()
                and split_marker.read_text(encoding="utf-8") == key
            ):
                transcript = _transcribe_chunks(audio, client, directory, key, language)
            else:
                try:
                    transcript = client.transcribe(audio)
                except ExternalCallError as exc:
                    if exc.http_status != 504 or audio_duration(audio) <= CHUNK_SECONDS:
                        raise
                    atomic_text(split_marker, key)
                    transcript = _transcribe_chunks(
                        audio, client, directory, key, language
                    )
            transcript = transcript.model_copy(update={"input_hash": key})
            atomic_text(output, transcript.model_dump_json(indent=2) + "\n")
            reused = False
    _record_speech(
        settings, source_id, transcript, language, force_language=force_language
    )
    return transcript, reused


def transcribe(
    source_id: Annotated[int, typer.Argument(min=1)],
    language: Annotated[
        str | None, typer.Option(help="Deepgram language code, such as en or ar-EG.")
    ] = None,
    budget: BudgetOverride = None,
    force_language: Annotated[
        bool,
        typer.Option("--force-language", help="Continue when detection disagrees."),
    ] = False,
    wait: Annotated[
        float,
        typer.Option("--wait", min=0, help="Seconds to wait if this source is locked."),
    ] = 0,
) -> None:
    """Transcribe a permitted source with word timestamps."""
    try:
        settings = Settings.from_env()
        transcript, reused = transcribe_source(
            settings,
            source_id,
            language=language or settings.deepgram_language,
            budget=Budget.from_settings(settings, budget),
            force_language=force_language,
            wait=wait,
        )
    except SpeechError as exc:
        typer.echo(f"Transcription failed: {exc}", err=True)
        raise typer.Exit(exc.code) from exc
    except (
        BudgetExceeded,
        ExternalCallError,
        MediaError,
        OSError,
        ValueError,
        sqlite3.Error,
        wave.Error,
    ) as exc:
        typer.echo(f"Transcription failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(
        f"Source {source_id}: {len(transcript.words)} words {'reused' if reused else 'transcribed'}."
    )
