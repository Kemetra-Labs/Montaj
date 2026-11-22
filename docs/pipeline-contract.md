# Pipeline contract

The source stages run through ranking. Ranking advances selected clips from `candidate` to `ranked`. Clip stages then progress through `rendered`, `captioned`, `drafted` and `checked`. A rule failure can mark a clip `needs_fix` or `rejected`. Human review records `approved` or `rejected`. Export and manual post logging record `exported` and `posted`.

`raw_path` points to the uncaptioned render. `final_path` points to the captioned render. Store both relative to the configured data directory so the workspace can move. Save stage results with their input hashes only after their artifacts are written successfully. Before reusing a matching hash, confirm the corresponding artifact still exists.

The cut hash includes the source, trim and render settings. Captioning also consumes the transcript. A trim invalidates both renders and any draft that quotes the clip. A hook or caption edit invalidates the check. A campaign rule change invalidates the check. Each caller includes any additional stage-specific inputs, such as prompt or model settings, in `ClipInputs.stage_options`.

`check_result` and `check_input_hash` record the latest rule check. `reviewed_at` and `reviewed_input_hash` record the review decision and the content reviewed. A plain rerun preserves human decisions. A content change after approval returns the clip to `drafted` and sets `changed_since_review`. Export requires a fresh approval for the current content and a current check without failures.

Use `is_stale` to compare the current inputs against a stored result. Use `next_stage` to find the earliest pending clip stage. A posted clip is terminal.
