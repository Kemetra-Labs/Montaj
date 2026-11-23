# MJ-6 through MJ-9 Feasibility Implementation Plan

**Goal**  Collect reproducible evidence before adding optional speech, job queue, or editorial dependencies to Montaj v1.

**Architecture**  Keep the pilots in `scripts/` and use saved artifacts and synthetic fixtures. Keep the speech scorer independent of provider libraries. Use the real OTIO adapter in a disposable environment and exercise the existing job worker with fake production work.

**Tech stack**  Python 3.12, pytest, Montaj transcript schema, SQLite, optional OpenTimelineIO and local speech runtimes.

**Spec**  [MJ-6](https://rahmaqanater.atlassian.net/browse/MJ-6), [MJ-7](https://rahmaqanater.atlassian.net/browse/MJ-7), [MJ-8](https://rahmaqanater.atlassian.net/browse/MJ-8), and [MJ-9](https://rahmaqanater.atlassian.net/browse/MJ-9).

## Constraints

Use English fixtures, existing caches read-only, and a disposable directory for generated output. Run everything offline after acquiring public test dependencies. Leave the pipeline, paid-call path, defaults, and approval flow untouched. Record unavailable measurements and dependencies explicitly.

## Tasks

- [x] Write regressions for transcript error counts, matched word timing, false speech, and swapped speaker labels. Implement a scorer for excerpt-relative word and activity data. Run it against the available synthetic fixture and any local model output whose provenance can be verified. Inspect the Deepgram cache for an independent annotated match.
- [x] Verify dashboard restart behavior with fake work and source checkpoints. Compare it with a disposable SQLite Huey queue or primary documentation when execution is unavailable. Document the operator recovery path and decision.
- [x] Write tests for an OTIO handoff to DaVinci Resolve using same-rate synthetic rendered clips. Verify relative references, exact frame ranges, adapter round-trip, and missing-media rejection. Confirm actual demand and editor execution separately from adapter verification.
- [ ] Record per-tool decisions and reproducible commands in `docs/engineering/mj-6-to-mj-9-feasibility.md`. Link the Tool Radar. Run local checks, request one branch review, open the PR, wait for required checks, and merge the reviewed head.

## Review focus

Check whether reported accuracy has independently annotated evidence, whether the scorer handles missing and overlapping speech, whether the restart exercise can touch real jobs, and whether OTIO paths stay inside a portable test folder and preserve approval state.
