"""Benchmark local English speech tools on Montaj's synthetic speech fixture."""

import argparse
import json
import subprocess
import wave
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

from montaj.transcribe import Transcript, Word
from scripts.speech_probe import Span, score_activity, score_words

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
RATE = 16_000
EXCERPTS = ((0, 4.16), (4.16, 8.31), (8.31, 11.03), (11.03, 13.67), (13.67, 18))


def _pcm_samples(audio: Path):
    import numpy as np

    with wave.open(str(audio), "rb") as stream:
        samples = stream.readframes(stream.getnframes())
    return np.frombuffer(samples, dtype="<i2").astype("float32") / 32768


def _faster_whisper(model: Path):
    if not (model / "tokenizer.json").is_file():
        raise ValueError(
            "faster-whisper needs a local tokenizer.json in the model folder."
        )
    from faster_whisper import WhisperModel

    engine = WhisperModel(
        str(model),
        device="cpu",
        compute_type="int8",
        cpu_threads=4,
        local_files_only=True,
    )

    def transcribe(audio: Path) -> list[Word]:
        segments, _ = engine.transcribe(
            _pcm_samples(audio), language="en", beam_size=1, word_timestamps=True
        )
        return [
            Word(text=word.word.strip(), start=word.start, end=word.end)
            for segment in segments
            for word in segment.words or []
            if word.word.strip()
        ]

    return transcribe


def _whisper_cpp(model: Path, executable: Path):
    def transcribe(audio: Path) -> list[Word]:
        subprocess.run(
            [
                str(executable),
                "-m",
                str(model),
                "-f",
                str(audio),
                "-l",
                "en",
                "-t",
                "4",
                "-bs",
                "1",
                "-ng",
                "-ml",
                "1",
                "-sow",
                "-oj",
                "-of",
                str(audio),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        result = json.loads(audio.with_suffix(".wav.json").read_bytes())
        return [
            Word(
                text=part["text"].strip(),
                start=part["offsets"]["from"] / 1000,
                end=part["offsets"]["to"] / 1000,
            )
            for part in result["transcription"]
            if part["text"].strip()
        ]

    return transcribe


def _silero(model: Path):
    import numpy as np
    import onnxruntime as ort

    options = ort.SessionOptions()
    options.inter_op_num_threads = options.intra_op_num_threads = 1
    engine = ort.InferenceSession(
        str(model), sess_options=options, providers=["CPUExecutionProvider"]
    )

    def detect(audio: Path) -> list[Span]:
        samples = _pcm_samples(audio)
        state = np.zeros((2, 1, 128), dtype="float32")
        context = np.zeros(64, dtype="float32")
        sample_rate = np.array(RATE, dtype="int64")
        start = None
        spans: list[Span] = []
        for index in range(0, len(samples), 512):
            chunk = samples[index : index + 512]
            data = np.concatenate((context, np.pad(chunk, (0, 512 - len(chunk)))))
            probability, state = engine.run(
                None, {"input": data[None, :], "state": state, "sr": sample_rate}
            )
            context = data[-64:]
            if probability.item() >= 0.5 and start is None:
                start = index
            elif probability.item() < 0.5 and start is not None:
                spans.append(Span(start=start / RATE, end=index / RATE))
                start = None
        if start is not None:
            spans.append(Span(start=start / RATE, end=len(samples) / RATE))
        return spans

    return detect


def _excerpt(source: Path, output: Path, start: float, end: float) -> None:
    with wave.open(str(source), "rb") as original, wave.open(str(output), "wb") as clip:
        clip.setparams(original.getparams())
        original.setpos(round(start * RATE))
        clip.writeframes(original.readframes(round(end * RATE) - round(start * RATE)))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("provider", choices=("faster-whisper", "whisper.cpp", "silero"))
    parser.add_argument("model", type=Path)
    parser.add_argument("--whisper-cli", type=Path)
    parser.add_argument("--scratch", type=Path, required=True)
    args = parser.parse_args()
    model = args.model.resolve()
    if not model.exists() or (args.provider == "faster-whisper" and not model.is_dir()):
        parser.error("Provide a local model path.")
    if args.provider == "whisper.cpp" and (
        args.whisper_cli is None or not args.whisper_cli.is_file()
    ):
        parser.error("Provide the local whisper.cpp binary via --whisper-cli.")
    reference = Transcript.model_validate_json(
        (FIXTURES / "transcript.json").read_bytes()
    ).words
    started = perf_counter()
    if args.provider == "faster-whisper":
        process = _faster_whisper(model)
    elif args.provider == "whisper.cpp":
        process = _whisper_cpp(model, args.whisper_cli.resolve())
    else:
        process = _silero(model)
    setup_seconds = perf_counter() - started
    results = []
    with TemporaryDirectory(dir=args.scratch) as folder:
        source, audio = Path(folder) / "full.wav", Path(folder) / "excerpt.wav"
        subprocess.run(
            [
                "ffmpeg",
                "-loglevel",
                "error",
                "-i",
                str(FIXTURES / "sample.wav"),
                "-ar",
                str(RATE),
                "-ac",
                "1",
                "-c:a",
                "pcm_s16le",
                str(source),
            ],
            check=True,
        )
        for start, end in EXCERPTS:
            _excerpt(source, audio, start, end)
            expected = [
                word.model_copy(
                    update={"start": word.start - start, "end": word.end - start}
                )
                for word in reference
                if start <= word.start and word.end <= end
            ]
            started = perf_counter()
            output = process(audio)
            elapsed = perf_counter() - started
            if args.provider == "silero":
                # These synthetic words have 60 ms internal gaps. Treat the excerpt as one utterance.
                scores = score_activity(
                    [Span(start=expected[0].start, end=expected[-1].end)], output
                )
            else:
                scores = score_words(expected, output)
            results.append(
                {
                    "start": start,
                    "end": end,
                    "elapsed_seconds": elapsed,
                    "real_time_factor": elapsed / (end - start),
                    **scores,
                }
            )
    print(
        json.dumps(
            {
                "provider": args.provider,
                "setup_seconds": setup_seconds,
                "excerpts": results,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
