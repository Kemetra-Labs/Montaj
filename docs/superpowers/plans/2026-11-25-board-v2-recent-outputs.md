# Board v2 recent outputs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the "Recent outputs" placeholder on the board home with the five newest rendered clips across sources: a thumbnail block with the duration, the title, "file · resolution · age", a status chip, a check note in fixed wording, and one link into the clip preview. No review decisions on the board.

**Architecture:** `board_view.recent_outputs` (MJ-22) gains a permission flag and a fixed-wording check note in place of the raw reason. A new `src/montaj/dashboard_outputs.py` renders the panel. `dashboard_response` loads the five records inside its existing error guard and passes them through `build_view(outputs=...)`. The clip preview gets one anchor per clip so a row can link to its clip.

**Tech Stack:** Python 3.12, SQLite, server-rendered HTML strings, `dashboard.css`, pytest, ruff, mypy.

**Spec:** Jira [MJ-25](https://rahmaqanater.atlassian.net/browse/MJ-25), epic [MJ-21](https://rahmaqanater.atlassian.net/browse/MJ-21), and the MJ-22 note: "`recent_outputs` includes clips of sources that are not permitted and carries no permitted flag. Do not offer review or export links for them." `DESIGN.md` for row, chip and button rules.

## Global Constraints

- Review decisions stay in the clip preview: no approve, reject or export control on the board. Approval and export still need a current check without failures (unchanged code).
- A clip of a source that is not permitted gets no link; its row says why.
- A check note is fixed wording chosen from the rule that failed, never stored text (the stored `decision_reason` can hold a ranking reason or other free text).
- Queries are read only. Every dynamic string is HTML-escaped.
- No state carried by colour alone; colours and type from `DESIGN.md` tokens only.
- Before each commit: `.venv\Scripts\python.exe -m ruff check . ; ... ruff format --check . ; ... mypy src ; ... pytest` (`uv run` hangs here). Known on a clean `main`: 3 mypy import errors (cv2, numpy) and two environmental test failures (`test_engine::test_recipe_tracks_vision_availability_niche_and_loudness`, `test_speech::test_limits_download_errors_and_permission_basis`).
- Commit messages are short and imperative; commits end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. One PR for MJ-25; the body says "MJ-25".

## Calls made while writing this (the owner can overrule)

- **Clips of sources that are not permitted are shown, with no link.** The row keeps its title and status, the chip reads "Blocked", and the note gives the permission reason ("Record permission for this source first." or the basis variant) (the MJ-22 note asked for no links; hiding them would make a clip vanish without saying why).
- **Chips:** "Check failed" (warning) when the check failed; "Ready for review" (review) for a checked clip; "Approved" (success); "Exported" (success) for exported or posted; "Not checked yet" (neutral) for rendered, captioned or drafted clips; "Blocked" (warning) first, for a source that is not permitted. The ticket names the first two; the rest cover the other rendered statuses.
- **One link per row:** "See check" for a failed check, else "Open review". Both go to `/?source=<id>#clip-<clip id>`; the clip preview gains `id="clip-<id>"` on each clip so the link lands on the clip.
- **Fixed check wording**, by the prefix of each stored rule reason (`check.py`): "Length …" → "Length is outside the allowed range."; "Missing required hashtag" → "A required hashtag is missing."; "Missing disclosure" → "The disclosure is missing."; "Matches banned word or topic" → "Uses a banned word or topic."; "Source is not permitted." → "The source is not permitted."; "This clip was already posted." → same; "No matching account" → "No account matches the daily post cap."; "Possible medical claim" → "Possible medical claim or promise."; anything else → "Open the clip preview to see why." Distinct notes in order, joined with a space.
- **Resolution** is `1080×1920` for every render: both cut styles in `engine.py` (`CUT_STYLE`, `CUT_STYLE_V2`) say so. A test ties the constant to those two strings so a new style fails it.
- **Age** is the media file's modified time (MJ-22's `finished_at`): "just now", "N min ago", "N h ago", "N d ago"; nothing when unknown.
- **Grid retune.** The cards no longer need the old table's width, so the columns become `minmax(0, 1.6fr) minmax(17rem, 1fr)`; one column below 60rem stays.
- **MJ-22 deferred minors folded in** because the board now calls `recent_outputs` on every load: a path with a NUL byte or a symlink loop gives no age instead of raising (a raise would turn the whole board into the 503 page); a whitespace-only `hook_text` falls back to `hook_a`; `limit` below 1 is refused.

## Review Focus

1. A clip of a source that is not permitted has no link of any kind, and its row says why. Task 2 pins it.
2. A stored reason that is not a known rule reason (a ranking reason, a stack trace, HTML) never reaches the page. Task 1 pins the mapping; Task 2 pins escaping.
3. A clip whose media path is odd (missing, outside the data directory, NUL byte) still gives a row, and the board never becomes the 503 page because of it. Task 1 pins it.
4. With no rendered clips, the panel says "No clips yet" with the next step; with more than five, only the five newest show. Task 3 pins it.
5. At 375px the panel stacks under the cards without sideways page scroll, and long titles wrap. Task 3 checks it.

---

## File structure

- Modify `src/montaj/board_view.py`: `OutputRecord.blocked_reason`, `OutputRecord.check_note` (replaces `check_reason`), `check_note(reason)`, `OUTPUT_SIZE`, hardening.
- Create `src/montaj/dashboard_outputs.py`: `outputs_html`, `output_chip`, `age_text`, `duration_text`.
- Modify `src/montaj/dashboard.py` (load and render), `src/montaj/preview.py` (clip anchors), `src/montaj/dashboard.css`, `DESIGN.md`.
- Tests: `tests/test_board_outputs.py`, create `tests/test_dashboard_outputs.py`, `tests/test_dashboard_board.py`, the preview test file that renders clips.

---

### Task 1: Records: permission, fixed check wording, hardening

**Files:** Modify `src/montaj/board_view.py`; Test `tests/test_board_outputs.py`.

**Interfaces:**
- Consumes: MJ-22 `OutputRecord`, `recent_outputs`, `_modified`, `_output`; `dashboard_parts.permission_reason`.
- Produces: `OutputRecord` fields `clip_id, source_id, title, file, duration_s, status, check_failed, check_note, finished_at, blocked_reason` (`check_reason` is gone; `blocked_reason` is `permission_reason(...)`, empty when the source is permitted); `check_note(reason: str | None) -> str`; `OUTPUT_SIZE = "1080×1920"`; `recent_outputs(settings, limit=5)` raises `ValueError` for `limit < 1`.

- [ ] **Step 1: Write the failing tests** in `tests/test_board_outputs.py`. Replace `test_failed_check_reason_is_shortened` and update the `check_reason` assertions in `test_record_fields` and `test_ranking_reason_is_not_a_check_reason` to `check_note`. Add:
  - `test_check_note_uses_fixed_wording` (parametrized): `"Length 75.00s is outside 20.00-60.00s."` → `"Length is outside the allowed range."`; `"Missing required hashtag: #ad"` → `"A required hashtag is missing."`; `"Missing disclosure: Paid"` → `"The disclosure is missing."`; `"Matches banned word or topic: x"` → `"Uses a banned word or topic."`; `"Source is not permitted."`, `"This clip was already posted."`, `"No matching account to apply the daily post cap."`, `"Possible medical claim or promise: cure"` to their fixed sentences; `"Strong opening line"`, `"Traceback (most recent call last)"` and `"<script>x</script>"` → `"Open the clip preview to see why."`; two reasons joined by `"; "` give two sentences; a repeated reason gives one; `None` and `""` give `""`.
  - `test_failed_record_carries_a_fixed_note`: a `NEEDS_FIX` clip with `check_result=FAIL` and `decision_reason="Length 75.00s is outside 20.00-60.00s.; Matches banned word or topic: x"` has `check_note == "Length is outside the allowed range. Uses a banned word or topic."`; a passing clip has `""`.
  - `test_record_carries_the_permission_reason`: clips of a permitted source, a source with `source_allowed=False`, and one with a blank basis give `blocked_reason` `""`, "Record permission for this source first." and "Record a permission basis for this source first."
  - `test_odd_media_paths_give_a_record_without_an_age` (Review Focus 3): `final_path` of `"a\x00b.mp4"` gives a record with `finished_at is None` and no exception.
  - `test_blank_hook_text_falls_back_to_hook_a`: `hook_text="  "`, `hook_a="Hook A"` gives title `"Hook A"`.
  - `test_limit_must_be_positive`: `recent_outputs(settings, 0)` raises `ValueError`.
  - `test_output_size_matches_the_cut_styles`: `"1080x1920"` appears in both `engine.CUT_STYLE` and `engine.CUT_STYLE_V2`, and `OUTPUT_SIZE == "1080×1920"`.
- [ ] **Step 2: Run and watch them fail.** Run: `.venv\Scripts\python.exe -m pytest tests/test_board_outputs.py -q`. Expected: FAIL on the missing names and fields.
- [ ] **Step 3: Implement.** `check_note` maps by `startswith` on each `"; "`-separated reason. `recent_outputs` reads permission with one more read-only query (`SELECT id, source_allowed, allowed_basis FROM sources WHERE id IN (…)` with bound parameters) and uses `permission_reason`. `_modified` puts `resolve`, `is_relative_to`, `is_symlink`, `is_file` and `stat` under one `try` catching `(OSError, ValueError, RuntimeError)`. Delete `_short` and `_REASON_MAX` if nothing else uses them.
- [ ] **Step 4: Run the tests.** Expected: PASS.
- [ ] **Step 5: Gate and commit.** Commit: `Give recent outputs a permission flag and fixed check wording`.

---

### Task 2: Recent outputs renderer

**Files:** Create `src/montaj/dashboard_outputs.py`; Test `tests/test_dashboard_outputs.py` (create); Modify `src/montaj/dashboard.css`.

**Interfaces:**
- Consumes: Task 1 `OutputRecord`, `OUTPUT_SIZE`; `dashboard_parts.badge`, `link_button`; `models.ClipStatus`.
- Produces: `outputs_html(outputs: tuple[OutputRecord, ...], now: datetime) -> str`, the inside of the panel: a muted heading note "Approve and reject happen in the clip preview.", then `<ol class="outputs" aria-label="Recent outputs">` of `<li class="output">` rows, or the empty state; `output_chip(record) -> Chip` (reuse `board_view.Chip`); `age_text(finished: datetime | None, now: datetime) -> str`; `duration_text(seconds: float) -> str` (`"42s"`, `"1m 05s"`).
- Row markup the tests pin: `<div class="thumb" aria-hidden="true">{duration}</div>`; the escaped title; `<p class="secondary">{file} · 1080×1920 · {age}</p>` (parts that are empty are left out with their separator); the chip `<span class="badge {tone}">{label}</span>`; the note in `<p class="output-note">`, warning ink when the check failed; the link `link_button("/?source={id}#clip-{clip}", label)`.

- [ ] **Step 1: Write the failing tests** in `tests/test_dashboard_outputs.py`, building `OutputRecord`s directly (no database) with a fixed `now`:
  - `test_ready_row`: a checked, passing, permitted clip shows its title, `final.mp4 · 1080×1920 · 3 min ago`, the chip `Ready for review`, and `href="/?source=7#clip-4">Open review</a>`.
  - `test_failed_row_shows_the_fixed_reason`: a failed clip shows `Check failed`, its `check_note` in `class="output-note warning"`, and `See check`.
  - `test_blocked_row_has_no_link` (Review Focus 1): a record with `blocked_reason="Record permission for this source first."` shows `Blocked`, that reason, and no `<a class="button` and no `href="/?source=`.
  - `test_chip_for_each_status` (parametrized): `APPROVED` → `Approved`, `EXPORTED` and `POSTED` → `Exported`, `RENDERED`/`CAPTIONED`/`DRAFTED` → `Not checked yet`.
  - `test_text_is_escaped` (Review Focus 2): a title and file of `<b>x</b>` render escaped.
  - `test_age_and_duration_text`: `age_text` for 20 s, 5 min, 3 h, 2 d and `None`; `duration_text(42)`, `duration_text(65)`.
  - `test_empty_state`: no records gives "No clips yet" and the next step "Clips appear here once a source is cut and captioned."
  - `test_heading_note_and_no_review_controls`: the output has "Approve and reject happen in the clip preview." and no `<form` and no `<button`.
- [ ] **Step 2: Run and watch them fail.** Expected: FAIL on the missing module.
- [ ] **Step 3: Implement** `dashboard_outputs.py`. Chip order: Blocked first, then Check failed, then by status.
- [ ] **Step 4: CSS**, existing tokens only: `.outputs` (list reset, hairlines), `.output` (grid: a `3rem` `.thumb` then the body; the chip and link on one wrapping row), `.output-note` (muted, `.76rem`; `.output-note.warning` in `--warning`), titles wrap anywhere. Reuse `.thumb`.
- [ ] **Step 5: Gate and commit.** Commit: `Render the recent outputs rows`.

---

### Task 3: Wire the panel, link anchors, check it

**Files:** Modify `src/montaj/dashboard.py`, `src/montaj/preview.py`, `src/montaj/dashboard.css`, `DESIGN.md`; Test `tests/test_dashboard_board.py` and the preview tests.

**Interfaces:**
- Consumes: Tasks 1 and 2.
- Produces: `dashboard_response` calls `recent_outputs(settings, 5)` inside its existing `try` (only for the home, not source pages) and passes the tuple to `build_view(outputs=...)`; `_overview` renders `<section class="panel recent">` with `<h2>Recent outputs</h2>` and `outputs_html(data.outputs, datetime.now())`; the clip preview's `<article>` gets `id="clip-{clip_id}"`.

- [ ] **Step 1: Write the failing tests.**
  - `test_home_shows_the_five_newest_outputs` (Review Focus 4): seven rendered clips: the home's recent panel lists clip ids 7 to 3 in that order and not 2 or 1.
  - `test_home_empty_outputs`: no clips: "No clips yet" is on the home and "Nothing to show yet." is gone.
  - `test_home_outputs_link_to_the_clip`: the row's link is `/?source=7#clip-<id>` and the clip preview at `/?source=7` contains `id="clip-<id>"`.
  - `test_unreadable_media_path_does_not_break_the_board` (Review Focus 3): a clip with `final_path="a\x00b.mp4"` still gives a 200 board.
- [ ] **Step 2: Run and watch them fail.** Expected: FAIL (placeholder still rendered; no anchors).
- [ ] **Step 3: Implement** the loading, the panel and the anchor; retune `.pipeline` to `minmax(0, 1.6fr) minmax(17rem, 1fr)`.
- [ ] **Step 4: Run the tests.** Expected: PASS.
- [ ] **Step 5: `DESIGN.md`.** Add a **Recent output row** bullet (thumbnail with duration, title, file · resolution · age, chip, fixed-wording note, one link; no review decisions on the board).
- [ ] **Step 6: Visual check.** Seed a scratch database (never the real `data/`) with seven clips across a permitted and an unpermitted source: checked, failed with two reasons, approved, exported, drafted, a long HTML-looking title, and an unreadable media path. Run the board (`.venv\Scripts\python.exe -c "from montaj.cli import app; app()" dashboard --port 8765`). Screenshot at 1366px and 375px. Expected: five rows, every chip has words, blocked row has no link, the cards column still fits its content at 1366px, no sideways page scroll at 375px. Fix what fails; stop the server.
- [ ] **Step 7: Gate and commit.** Commit: `Show recent outputs on the board home`.

Final step for the executor: the whole-branch review (Opus), then the PR body per the repo's conventions (what changed, the "Calls made" above, the checks run, "MJ-25").
