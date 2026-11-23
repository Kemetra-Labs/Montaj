# MJ-6 through MJ-9 feasibility results

Montaj v1 stays English-first. The pilots use synthetic media or read-only inspection of existing metadata. Deepgram Nova-3 remains the default. The results are intended for the [Montaj Tool Radar](https://rahmaqanater.atlassian.net/wiki/spaces/MONTAJ/pages/90079250).

## MJ-6 · Offline speech and cost

Run on Windows with Python 3.12 and a Ryzen 7 9800X3D. The five excerpts are nonoverlapping cuts from [the 18-second synthetic English fixture](../../tests/fixtures/README.md), totaling 36 reference words. They check execution, scoring, and timing. They cover one synthesized voice and little silence. The local Deepgram cache is for different, real media. Five excerpts from the existing permitted English source provide a separate agreement check against that cache. A fair accuracy comparison still needs independently transcribed and timed excerpts. No database writes or paid API calls were made.

| Tool | Result on the fixture | Wall time and RAM | Decision |
| --- | --- | --- | --- |
| [faster-whisper 1.2.1](https://github.com/SYSTRAN/faster-whisper) with [MIT tiny.en weights](https://huggingface.co/Systran/faster-whisper-tiny.en/tree/0d3d19a) | 7/36 word errors (19.4%). Among 30 matched words, mean absolute start/end errors were 74/110 ms. | 1.33 s inference over 18 s of audio (0.074× real time), plus 0.33 s setup. About 269 MiB process peak. English model directory 78.1 MB. | **Defer production integration.** First candidate for a real-media comparison based on the closer word times and straightforward `Transcriber` mapping. |
| [whisper.cpp v1.8.3](https://github.com/ggml-org/whisper.cpp/releases/tag/v1.8.3) with [MIT tiny.en Q5_1 weights](https://huggingface.co/ggerganov/whisper.cpp) | 6/36 word errors (16.7%). Among 30 matched words, mean absolute start/end errors were 231/188 ms using word-split segments. | 2.64 s including CLI startup and model load for each excerpt (0.147× real time). About 120 MiB observed CLI peak. Model 30.7 MiB and Windows zip 3.8 MiB. | **Defer.** Keep as a lower-memory candidate and repeat with verified token-level alignment before comparing timing on real media. |
| [Silero VAD v6.2](https://github.com/snakers4/silero-vad/tree/v6.2) with its MIT repo ONNX artifact | 0.73 s missed speech and 0.22 s false speech over 15.20 s labeled speech. It matched all 10 excerpt boundaries within 250 ms (31 ms average absolute error) and emitted 30 extra boundaries. | 0.08 s inference (0.004× real time) plus 0.20 s setup. About 87 MiB process peak. ONNX model 2.2 MiB. | **Defer.** Benchmark hysteresis and minimum pause duration against existing caption boundaries on real speech before proposing edits. |

Both ASR runs used four CPU threads and beam size one. faster-whisper used CTranslate2 4.9.1, CPU int8 and the NumPy PCM input path. Its measured main packages took about 175 MiB on disk (faster-whisper, CTranslate2, PyAV and NumPy, excluding weights and smaller dependencies). Silero used ONNX Runtime 1.32.0 and NumPy, approximately 88 MiB installed for those two packages, one thread, 32 ms windows, and a raw 0.5 threshold. Memory is peak process working set, sampled for the whisper.cpp child. FFmpeg resampling and metrics are excluded from inference time. The label for each VAD excerpt covers the utterance between first and last reference words, including the fixture's short internal word gaps. Start and end boundaries are greedily matched in chronological order to the first eligible boundary within 250 ms. The mean reflects those matches, so interpret it alongside the unmatched counts.

On 50.92 seconds of cached source audio (206 Deepgram words), faster-whisper differed by 40 normalized edits, and whisper.cpp differed by 41. These are **19.4% and 19.9% disagreement with Deepgram**, respectively. Among matched words, their mean absolute start/end differences from Deepgram were 98/82 ms (173 words) and 184/178 ms (174 words). The five roughly 10-second excerpts start near the 10th, 28th, 46th, 64th and 82nd percentiles of the cached word list. The single cached source, unknown Deepgram reference errors, and lack of independent word timing limit what this comparison says about quality. No media or transcript excerpts are included in this report.

The paid call remains inside `DeepgramTranscriber`. An eligible matching transcript is reused before the call. The cache key includes source audio, model, language and processing options. A future backend needs provider, runtime, model hash and settings in its own cache identity. Keep it separate from the existing hash and log any paid fallback under the normal budget. Reuse the current `run --estimate` preflight for the potential hosted charge per source. Local inference trades that charge for CPU time and model storage. The fixture does not establish realized savings or acceptable accuracy.

### Reproduce

Use a disposable Python 3.12 environment with Montaj's dependencies and FFmpeg on PATH. Install `faster-whisper==1.2.1`, `ctranslate2==4.9.1` and `onnxruntime==1.32.0` there. Obtain each public model from the sources above. The faster-whisper directory needs its own `tokenizer.json` so inference can stay offline. Extract the official v1.8.3 Windows x64 CLI, and use an existing scratch directory. Run from the repository root. In PowerShell with an uninstalled source checkout, set `$env:PYTHONPATH = 'src'` first.

```text
python -m scripts.synthetic_speech_benchmark faster-whisper <tiny.en-directory> --scratch <scratch>
python -m scripts.synthetic_speech_benchmark whisper.cpp <ggml-tiny.en-q5_1.bin> --scratch <scratch> --whisper-cli <whisper-cli.exe>
python -m scripts.synthetic_speech_benchmark silero <silero_vad.onnx> --scratch <scratch>
```

Model fingerprints used here are SHA-256 `1a5afae06a4db91c975c9a9d78be5cc110ee4ea022ad57d55492e4550e936b2a` for the CTranslate2 `model.bin`, the upstream SHA-1 `3fb92ec865cbbc769f08137f22470d6b66e071b6` for the GGML file, and SHA-256 `1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3` for Silero.

To score independently aligned artifacts, use `python -m scripts.speech_probe {words,vad,diarization} REFERENCE.json HYPOTHESIS.json`. Keep both JSON files inside the working directory. In `words` mode the inputs use Montaj's transcript schema and timestamps start at the excerpt origin. Activity inputs are JSON lists of `{"start": 0.0, "end": 1.0}` spans. Diarization adds integer `speaker` IDs and requires two reference speakers. It scores speaker time with label permutation and overlapping speech, plus matched and unmatched per-speaker turn boundaries.

## MJ-7 · Diarization and forced alignment

**Defer both.** [MJ-2 QA](https://rahmaqanater.atlassian.net/browse/MJ-2) is still open. The inspected English cache has speaker labels, but it does not include an independently annotated two-speaker excerpt. The scorer was verified on synthetic overlap and changed speaker IDs. That checks metric behavior. Real detection, timing improvement, CPU time and RAM remain unmeasured.

- [pyannote.audio](https://github.com/pyannote/pyannote-audio) is MIT. [Community-1 weights](https://huggingface.co/pyannote/speaker-diarization-community-1) are CC BY 4.0 and require acceptance of data-sharing terms and an access token before acquiring a local copy. The resulting copy can run offline. Access was not accepted for this pilot.
- [WhisperX](https://github.com/m-bain/whisperX) is BSD-2-Clause. Its [English alignment selection](https://github.com/m-bain/whisperX/blob/main/whisperx/alignment.py) prefers [TorchAudio's MIT Wav2Vec2 Base 960H weights](https://docs.pytorch.org/audio/stable/generated/torchaudio.pipelines.WAV2VEC2_ASR_BASE_960H.html) when available. Confirm the resolved artifact, including licenses on any fallback weights and the diarization model, before benchmarking. Windows CPU dependencies and quality need a measured run.
- For a future test, annotate turns, overlapping speech and word times on a permitted two-speaker English clip with saved Deepgram labels. Compare the same intervals using the scorer. Speaker IDs can mark when to reconsider the [MJ-4](https://rahmaqanater.atlassian.net/browse/MJ-4) crop. Mapping a voice to a face requires separate audiovisual evidence.

## MJ-8 · Dashboard restart and Huey

**Defer Huey.** A dashboard instance accepts one active job. A second submission is rejected while the slot is occupied. Recreating the dashboard job manager clears live status, and the operator can submit the same source ID to resume. The dashboard test verifies these behaviors with a fake batch. The pipeline tests verify cached stages and approved clips can be reused. A forced stop before a checkpoint is written can still require work to repeat. The OS releases a source lock when the process exits.

A disposable Windows experiment with [Huey 3.5.4](https://github.com/coleifer/huey) and `SqliteHuey` enqueued source 7 twice. Both copies survived a queue reopen. After removing one via `dequeue()` and discarding it before completion, reopening exposed only the other copy. Setup and two enqueues took about 10 ms. The database occupied 36 KiB plus 32 KiB shared memory, with the WAL empty at the final observation. Load and WAL growth can change these numbers. The test shows persistence for pending work, separate application deduplication, and the need for a recovery record for in-flight work. It did not run FFmpeg jobs under contention or measure a live consumer. [Huey consumer shutdown and delivery details](https://huey.readthedocs.io/en/latest/consumer.html#consumer-shutdown).

| Operation | Current Montaj | A future local Huey pilot |
| --- | --- | --- |
| Startup and status | Start one dashboard. Active progress belongs to that process. | Start a separate `huey_consumer` with one thread and a durable queue file. A durable job table would still be needed for progress and abandoned jobs. |
| Windows and Docker | Leave the dashboard running until work completes. After restart, resume by source ID. Keep `/app/data` mounted in Docker. | Run a single consumer with the same media and Montaj data accessible. Use a local disk or Docker volume for the queue DB, separate from Montaj's DB. Validate Ctrl+C and child cleanup on Windows. Validate Docker signal handling with `init: true`, a graceful signal, and time for FFmpeg to finish. |
| Duplicate jobs and DB contention | The dashboard limits submissions and the pipeline locks each source, applies budgets, and reuses stage artifacts. | Keep those guards. Add durable source dedupe and recovery for dequeued tasks before enabling a second worker. Exercise a dashboard reader plus queue and Montaj writers under load. Separate SQLite files isolate queue writes, while Montaj state writes still contend. |

Review this decision if the operator repeatedly loses the ability to resume or regularly needs independent jobs to queue after leaving the dashboard. For a known source, the existing terminal path is `montaj job --resume SOURCE_ID --top N`.

## MJ-9 · Editorial handoff

**Defer product export.** The pilot target is DaVinci Resolve. The concrete workflow to validate is handing approved, already-captioned renders to an editor for sequencing and audio polish. Demand for that workflow is unconfirmed while [MJ-2](https://rahmaqanater.atlassian.net/browse/MJ-2) is open. Blackmagic documents native `.otio` import and relinking, so this pilot uses the regular OpenTimelineIO JSON adapter and relative media references. The adapter round-trip was tested on Windows. Resolve import and relinking still need an editor session. [DaVinci Resolve manual](https://documents.blackmagicdesign.com/UserManuals/DaVinci_Resolve_19_Reference_Manual.pdf) · [OpenTimelineIO documentation](https://opentimelineio.readthedocs.io/en/latest/).

Install `opentimelineio==0.18.1` in a disposable environment. Run `python -m scripts.timeline_probe MANIFEST.json OUTPUT.otio`. Keep the manifest and timeline output inside the working directory. The sample manifest below uses two copies of the checked-in 216-frame, 12 fps video fixture. Place those copies beside the output in `media/`. The input describes rendered media ranges in integer frames with an exclusive out point and retains the source and clip IDs as metadata.

```json
{
  "fps": 12,
  "clips": [
    {"clip_id": 7, "source_id": 2, "approved": true, "path": "media/clip one.mp4", "media_frames": 216, "in_frame": 30, "out_frame": 90},
    {"clip_id": 8, "source_id": 2, "approved": true, "path": "media/two.mp4", "media_frames": 216, "in_frame": 0, "out_frame": 30}
  ]
}
```

The tests serialize and read the ranges, check that the second clip starts at frame 60 on both tracks, move the package, and verify the relative reference still points to the same bytes. Missing media, path traversal, invalid ranges and unapproved manifest entries are rejected. The pilot reads only the manifest and media paths. For a later production integration, include only clips eligible under Montaj's reviewed export path. Test imported audio, frame rates, Resolve relinking and missing-media handling with the actual operator before adopting.
