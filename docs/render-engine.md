# Render engine

The `cut` stage renders every new clip in one FFmpeg pass. The pass reframes the shot to 9:16, applies a safe loudness gain and adds the fades. `captions` then burns word-highlighted ASS captions into that render. This page covers what the engine does, when it falls back and how reviewed clips are protected. Jira MJ-3, MJ-4 and MJ-5 track this work.

## Pipeline inside `cut`

1. **Decode once for analysis.** The system FFmpeg decodes the trimmed clip to 320 px wide at 15 fps (`reframe.FFmpegFrames`). Analysis never depends on which codecs a Python wheel can decode. The pilot source is AV1.
2. **Scene cuts.** PySceneDetect's `AdaptiveDetector` reads the same frames with a minimum scene of four analysis frames.
3. **Faces.** OpenCV YuNet runs on every fourth analysis frame, about 3.75 times per second. Samples within 1.5 frames of a cut are discarded.
4. **Crop plan per shot.** `camera.plan_shot` is a pure function:
   - Faces below a score of 0.7, shorter than 7% of the frame (18% for `games` sources), or under 45% of the largest face's height are ignored.
   - If no reliable face appears in at least 60% of samples, the shot falls back.
   - All remaining faces are kept together. The engine never guesses who is speaking.
   - If the centered crop already contains the padded faces in every sample, it stays centered. This is the legacy geometry, so such shots cannot get worse.
   - Otherwise it uses a still crop if one position works for the whole shot.
   - Otherwise a camera follows the faces at no more than 0.2 frame widths per second, using a look-ahead and a smoothing pass. The path is checked again against every sample.
   - Faces wider than the crop, or detections that jump faster than the camera may move, fall back.
5. **Fallback layout.** Most sources fall back to the centered crop. `games` sources fall back to a full-width fit over a blurred fill, which keeps HUD and screen content.
6. **One FFmpeg render.** The crop position is a piecewise-linear `crop` x-expression in clip time. A `-filter_complex` overlay adds fit shots. Audio and video come from the same input in one encode, which keeps them in sync.
7. **Loudness.** See the loudness section below.
8. **Report.** `clips/<id>/render.json` records the engine, the fallback reason, each shot's layout, reason and keyframes, the loudness readings and the analysis time.

Any missing piece leaves the render on the centered crop, and the report says why. That includes no OpenCV, no PySceneDetect, a missing or altered model file, a non-landscape source, or an analysis error.

## Loudness

The safe policy is on by default: a −18 LUFS target, a −2.5 dBTP ceiling and a +12 dB gain cap. Each clip goes through these steps:

1. FFmpeg's EBU R128 `loudnorm` analysis measures the trimmed segment.
2. One constant gain is applied: `min(target − integrated, ceiling − true peak, cap)`.
3. The gain is never negative, and changes under 0.1 dB are skipped. Silence is left alone.
4. After the AAC encode, the decoded output is remeasured. If its true peak exceeds −1 dBTP, the clip is rendered once more with the gain reduced by the overshoot.

There is no compressor or limiter, so speech dynamics are unchanged. Clips with high crest factors stop at the peak ceiling before they reach the target.

| Variable | Default | Meaning |
| --- | --- | --- |
| `MONTAJ_LOUDNESS_TARGET_LUFS` | −18 | −30 to −14, or `off` |
| `MONTAJ_LOUDNESS_CEILING_DBTP` | −2.5 | −6 to −1 |
| `MONTAJ_LOUDNESS_MAX_GAIN_DB` | 12 | 0 to 20 |

## Captions

The ASS engine is unchanged. Grouping and timing moved to `caption_layout.py`:

- **Size and outline.** 80 px with a 5 px outline, up from 66 and 4. The MJ-3 sweep measured 84 px fitting at 524 of 880 px.
- **Wrapping.** Lines wrap by estimated rendered width within the 880 px safe width, as well as by word count. Groups have at most two lines. A sentence end or a pause longer than 0.7 s closes a group.
- **Highlights.** Gaps of up to 0.5 s between words keep the caption on screen, so it no longer blinks off. The last word of a group lingers for 0.3 s unless the next group starts sooner.
- **Placement.** The 450 px bottom margin keeps text above the platform controls.

A libass render test checks English and Arabic extents against these limits.

## Clip selection clean-up (MJ-5)

Claude Sonnet still proposes moments and Claude Haiku still ranks them. After word snapping, `boundaries.py` applies deterministic rules:

- **Start.** A start moves back to the beginning of its sentence when that sentence starts within 6 s. It moves forward only if no earlier start exists, and at most 4 s. A sentence start follows a sentence end or a pause of at least 0.6 s. "And" after a full stop is a valid start.
- **End.** An end prefers a sentence end within 8 s of the proposed end. Failing that it uses a pause within 3 s. It never stops on a dangling connector such as "and" or "because".
- **Duration.** Every edit stays inside the duration bounds narrowed by the campaign's rules.
- **Duplicates.** A pick is dropped if it shares at least 3 s, or at least 15% of the shorter pick, with one already kept. It is also dropped if its wording repeats an earlier pick (3-word shingles, at least 55% shared). Picks whose edges did not need repair are kept first.

Raw model picks stay cached under the original key. Changing the clean-up re-derives candidates from that cache without a new paid request.

## Protecting reviewed clips

The render recipe is part of the cut fingerprint. It includes the engine version, the OpenCV, PySceneDetect and model versions, the fallback layout and the loudness policy. Any change re-renders unreviewed clips.

A clip that has a review decision, or is approved, exported or posted, stays pinned to the recipe it was rendered with. That covers both the old centered recipe and the old caption style (`pipeline.LegacyRecipe`), so turning on the new default never re-renders it or reopens its review.

The pin lasts while the stored hashes still match the old recipe. A trim or source change releases it as before. So does an explicit upgrade:

```powershell
uv run montaj cut 1 --clip 36 --upgrade
```

An upgraded clip returns to `drafted` with `changed_since_review` set and needs a fresh review and check before export. Files already exported stay where export put them.

## Rollback and setup

| Setting | Effect |
| --- | --- |
| `MONTAJ_ENGINE=legacy` | Previous centered recipe, no loudness gain, for new renders. An internal rollback switch, not a per-job choice. |
| `MONTAJ_YUNET_MODEL` | Path to the face model. The default is `data/models/face_detection_yunet_2023mar.onnx`. |

Smart reframing is an optional extra. A basic install has neither the packages nor the weight, and renders centered.

```powershell
uv sync --extra vision
```

```powershell
uv run montaj fetch-model
```

```powershell
uv run montaj doctor
```

`fetch-model` downloads the pinned weight once and verifies its SHA-256 before saving. Renders never download anything. `doctor` reports `smart reframing` as OK or as a warning with the reason.

## Licenses

All are read from the installed package metadata.

| Component | License | Notes |
| --- | --- | --- |
| opencv-python-headless 4.14 | Apache-2.0 | The wheel redistributes FFmpeg under LGPL (see its `LICENSE-3RD-PARTY.txt`). Montaj decodes through the system FFmpeg, not the bundled one. |
| PySceneDetect 0.6.7 | BSD-3-Clause | Pulls in click (BSD-3-Clause), tqdm (MPL-2.0 and MIT), platformdirs (MIT) and numpy (BSD-3-Clause and others). |
| YuNet `face_detection_yunet_2023mar.onnx` | MIT | From [opencv_zoo](https://github.com/opencv/opencv_zoo/tree/f12e12798e8314f7c074a6656816c048dcc95b7a/models/face_detection_yunet) at commit `f12e127`, SHA-256 `8f2383e4…52fa4`. It is not committed. It lives under the git-ignored `data/models/`. |
| FFmpeg | depends on the build | The system executable, as before. |

OpenShorts was a design reference only, for scene resets and full-width fallback. No code was copied.

## Performance

The engine follows Windows CPU limits:

- Analysis decodes at 320 px and 15 fps with two FFmpeg threads.
- OpenCV runs with two threads.
- The x264 encode keeps `-threads:v 2`.
- Fit shots add a quarter-resolution blur branch only when a clip has such a shot.

Analysis time per clip is recorded in `render.json`.

## Known limits

- **Speaker identity.** Nothing decides who is speaking. Two people far apart fall back to the center rather than choosing one.
- **Cut timing.** Scene cuts are timed to the 15 fps analysis grid. A crop switch can land up to one output frame early or late.
- **Validation evidence.** Containment was measured against YuNet detections, not human labels. The original MJ-4 hand labels were not recoverable. Real-world quality acceptance still needs human playback on a phone and a listening review.
