# Board v2 view model and money format Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Put the board's data (summary figures, source cards, recent outputs) in plain functions and dataclasses apart from HTML, and print dollar amounts as money instead of six decimals. No visual change.

**Architecture:** A new `src/montaj/board_view.py` turns the `BoardRow` list from `board.build_rows` into a frozen `BoardView`, with no I/O except one separate query function for recent outputs. `format_usd` lives in `dashboard_parts.py` next to the other markup primitives. The old overview pages call both, so later tasks only replace the rendering.

**Tech Stack:** Python 3.12, pydantic models, SQLite via `montaj.db.connect`, pytest, ruff, mypy.

**Spec:** Jira [MJ-22](https://rahmaqanater.atlassian.net/browse/MJ-22), part of epic [MJ-21](https://rahmaqanater.atlassian.net/browse/MJ-21). There is no spec file in the repo; the tickets are the spec.

## Global Constraints

- Only process sources marked `source_allowed`; a view never adds an action for a source `permission_reason` blocks.
- Every query is read only (`connect(settings.db_path, read_only=True)`).
- Type hints everywhere; frozen dataclasses with `slots=True` for anything crossing a function boundary, like `board.py`.
- Tests make no real API calls and use `db_path` and `tests/dashboard_helpers.py`.
- Before each commit: `uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest`.
- Commit messages are short and imperative. This work is one PR for MJ-22; the PR body says "MJ-22".

## Calls made while writing this (the owner can overrule)

- **No `chip` on `SourceCard`.** MJ-22 lists a chip on the card. The chip names (Running, Needs your confirm, Blocked, Failed, Ready for review, Done) are a presentation choice that MJ-24 renders, so the card carries `state`, `tone` and the stage segments it is derived from. Update MJ-22's wording when merging.
- **`format_usd` rule.** `$0.00` for zero, `$12.47` from one cent up, `$0.004` between half a tenth of a cent and one cent, and `<$0.001` for anything smaller but above zero, so real spend is never shown as free.
- **Only the six listed sites change.** Estimates and budget-error text built in `dashboard_jobs.py`, `check_budget` and the CLI keep six decimals.
- **"Age" of an output** is the modified time of its media file, because `clips` has no timestamp column. It is `None` when the file is missing or outside the data directory.
- **Output order** is clip id, newest first.
- **`Post log` is always pending** until MJ-30 wires real state.

## Review Focus

1. A source with permission missing but `checked` clips must not count as awaiting review (today's rule). Task 2 pins it.
2. A tiny non-zero cost must never print as `$0.00`. Task 1 pins it.
3. A source whose cost is over its ceiling must show `$0.00` left, never negative. Task 2 pins it.
4. A clip with no hook text, no media path, or a media path outside `data_dir` must still produce a record. Task 3 pins it.
5. A long or odd check reason must be shortened and left unescaped for the renderer. Task 3 pins it.

---

## File structure

- Modify `src/montaj/dashboard_parts.py`: add `format_usd`.
- Modify `src/montaj/dashboard.py` and `src/montaj/dashboard_detail.py`: use `format_usd`; the overview reads `Summary`.
- Create `src/montaj/board_view.py`: `STAGES`, predicates, `StageSegment`, `SourceCard`, `OutputRecord`, `Summary`, `BoardView`, `stage_segments`, `build_view`, `recent_outputs`.
- Create `tests/test_dashboard_money.py`, `tests/test_board_view.py`.
- Modify the five existing dashboard tests whose assertions held six-decimal money.

---

### Task 1: `format_usd` and the six call sites

**Files:**
- Modify: `src/montaj/dashboard_parts.py`
- Modify: `src/montaj/dashboard.py` (lines 224, 256, 259)
- Modify: `src/montaj/dashboard_detail.py` (lines 145, 184, 237)
- Test: `tests/test_dashboard_money.py` (create)
- Modify: `tests/test_dashboard.py:118`, `tests/test_dashboard_board.py:104,108`, `tests/test_dashboard_detail.py:159,166`, `tests/test_dashboard_jobs.py:400`

**Interfaces:**
- Produces: `format_usd(amount: float) -> str` in `montaj.dashboard_parts`. Raises `ValueError` for a negative or non-finite amount.

- [ ] **Step 1: Write the failing test** in `tests/test_dashboard_money.py`:

```python
import math

import pytest

from montaj.dashboard_parts import format_usd


@pytest.mark.parametrize(
    ("amount", "text"),
    [
        (0, "$0.00"),
        (0.0001, "<$0.001"),
        (0.00049, "<$0.001"),
        (0.0005, "$0.001"),
        (0.004, "$0.004"),
        (0.01, "$0.01"),
        (0.012345, "$0.01"),
        (0.5, "$0.50"),
        (12.4666, "$12.47"),
    ],
)
def test_format_usd(amount: float, text: str) -> None:
    assert format_usd(amount) == text


@pytest.mark.parametrize("amount", [-0.01, math.nan, math.inf])
def test_format_usd_rejects_negative_and_non_finite(amount: float) -> None:
    with pytest.raises(ValueError, match="amount"):
        format_usd(amount)
```

- [ ] **Step 2: Run it and see it fail.** Run: `uv run pytest tests/test_dashboard_money.py -v`. Expected: FAIL with `ImportError: cannot import name 'format_usd'`.

- [ ] **Step 3: Implement `format_usd(amount: float) -> str` in `src/montaj/dashboard_parts.py`.** Check `math.isfinite(amount) and amount >= 0` first and raise `ValueError("Cost amount must be a finite, non-negative number.")`. Then branch on the rule in the Calls section; use `f"${amount:.3f}"` only for the half-a-tenth-of-a-cent to one-cent range and `f"${amount:.2f}"` otherwise.

- [ ] **Step 4: Run the money tests and see them pass.** Run: `uv run pytest tests/test_dashboard_money.py -v`. Expected: PASS.

- [ ] **Step 5: Replace the six sites.** Import `format_usd` and replace `${x:.6f}` with `{format_usd(x)}` in the three f-strings in `dashboard.py` and the three in `dashboard_detail.py`. In `dashboard_detail.py:145` keep the `"unavailable"` branch when `live.spend is None`. Leave `:.2f` ceiling formatting as it is.

- [ ] **Step 6: Update the assertions that held six decimals** (these fail now, which proves the sites changed):
  - `tests/test_dashboard.py:118`: `b"$0.012345"` becomes `b"$0.01"`.
  - `tests/test_dashboard_board.py:104`: `<dd>$0.012345</dd>` becomes `<dd>$0.01</dd>`; line 108: `$0.500000 of $5.00 run ceiling` becomes `$0.50 of $5.00 run ceiling`.
  - `tests/test_dashboard_detail.py:159`: `$0.500000 of $2.00 source ceiling` becomes `$0.50 of $2.00 source ceiling`; line 166: `$0.250000` becomes `$0.25`.
  - `tests/test_dashboard_jobs.py:400`: `$0.250000` becomes `$0.25`.
  - Leave lines 211, 253-255, 385-396 and 493-499 alone: they check estimate and budget-error text the formatter does not touch.

- [ ] **Step 7: Run the dashboard tests.** Run: `uv run pytest tests/test_dashboard.py tests/test_dashboard_board.py tests/test_dashboard_detail.py tests/test_dashboard_jobs.py -q`. Expected: PASS.

- [ ] **Step 8: Commit.** `git add src/montaj/dashboard_parts.py src/montaj/dashboard.py src/montaj/dashboard_detail.py tests && git commit -m "Print board costs as money, not six decimals"`

---

### Task 2: Stage list, predicates, summary and cards

**Files:**
- Create: `src/montaj/board_view.py`
- Test: `tests/test_board_view.py` (create)

**Interfaces:**
- Consumes: `montaj.board` (`BoardRow`, `StageCell`, `CellState`, `NextAction`), `montaj.dashboard.SourceSnapshot`, `montaj.dashboard_parts.permission_reason`, `montaj.models.ClipStatus`.
- Produces, all in `montaj.board_view`:
  - `STAGES: tuple[tuple[str, str], ...]`: the 11 `(key, label)` pairs: `ingest/Ingest, transcribe/Transcribe, candidates/Candidates, rank/Rank, cut/Cut, captions/Captions, draft/Draft, check/Check, review/Review, export/Export, post_log/Post log`.
  - `needs_attention(row: BoardRow) -> bool` (`row.snapshot.tone == "warning"`).
  - `awaiting_review(row: BoardRow) -> bool` (no `permission_reason` and at least one `ClipStatus.CHECKED` clip).
  - `StageSegment(key: str, label: str, state: CellState, reason: str)`.
  - `SourceCard(source_id: int, title: str, niche: str, state: str, tone: str, segments: tuple[StageSegment, ...], action: NextAction | None, cost: float, ceiling: float, left: float, clips_to_review: int)`.
  - `Summary(spend: float, source_count: int, run_spend: float | None, run_ceiling: float, attention: int, review_clips: int)`.
  - `BoardView(summary: Summary, cards: tuple[SourceCard, ...], outputs: tuple[OutputRecord, ...])`.
  - `stage_segments(cells: tuple[StageCell, ...]) -> tuple[StageSegment, ...]`.
  - `build_view(rows: list[BoardRow], *, source_ceiling: float, run_ceiling: float, run_spend: tuple[float, float] | None = None, outputs: tuple[OutputRecord, ...] = ()) -> BoardView`. When `run_spend` is given it is `(spent, its ceiling)` and its ceiling replaces `run_ceiling`, as a job started with a budget override does today.
  - `OutputRecord` is defined here and filled in Task 3 (fields in Task 3).

- [ ] **Step 1: Write the failing tests** in `tests/test_board_view.py`. Build `SourceSnapshot` directly through a helper `_snapshot(**over)` (defaults: id 1, title "A", niche "Health", added "2026-11-24", allowed True, basis "Creator approval", state "Imported", tone "success", no problem, media "Imported", duration "1m 00s", checkpoints `()`, `clips=Counter()`, cost 0.0) and `BoardRow(snapshot, cells, action)`. Tests:
  - `test_stages_are_the_eleven_in_order`: `[key for key, _ in STAGES] == ["ingest","transcribe","candidates","rank","cut","captions","draft","check","review","export","post_log"]` and `STAGES[-1] == ("post_log", "Post log")`.
  - `test_group_state_is_shared_by_both_stages`: cells keyed `ingest, transcribe, rank, render, draft, review, export` with states DONE, DONE, RUNNING, PENDING, BLOCKED (reason `"Cut + captions first"`), BLOCKED, BLOCKED. Expect segment states `[DONE, DONE, RUNNING, RUNNING, PENDING, PENDING, BLOCKED, BLOCKED, BLOCKED, BLOCKED, PENDING]`, the `draft` and `check` segments both carrying the reason, and `post_log` pending with reason `""`.
  - `test_awaiting_review_ignores_unpermitted_source`: allowed False with one CHECKED clip gives `False`; allowed True with the same clip gives `True`; allowed True with none gives `False`.
  - `test_summary_counts_match_the_old_board`: three rows (A cost 0.5, tone "warning"; B cost 0.25, tone "review", allowed, two CHECKED; C cost 0, tone "neutral", unpermitted, one CHECKED). Expect `spend == 0.75`, `source_count == 3`, `attention == 1`, `review_clips == 2`, `run_spend is None`, `run_ceiling == 5.0` for `build_view(rows, source_ceiling=5.0, run_ceiling=5.0)`.
  - `test_running_job_ceiling_wins`: `run_spend=(0.5, 2.0)` with `run_ceiling=5.0` gives `summary.run_spend == 0.5` and `summary.run_ceiling == 2.0`.
  - `test_card_dollars_left_never_negative`: ceiling 1.0 with cost 0.4 gives `left == pytest.approx(0.6)`; cost 1.5 gives `left == 0.0`.
  - `test_empty_board`: `build_view([], source_ceiling=5.0, run_ceiling=5.0)` has zero summary figures and `cards == ()`.

- [ ] **Step 2: Run them and see them fail.** Run: `uv run pytest tests/test_board_view.py -v`. Expected: FAIL with `ModuleNotFoundError: No module named 'montaj.board_view'`.

- [ ] **Step 3: Implement the module** with the names above. `stage_segments` walks `STAGES`, looks up the cell whose key is the stage's group (`candidates` and `rank` take cell `rank`; `cut` and `captions` take `render`; `draft` and `check` take `draft`; the others take their own key) and copies its `state` and `reason`; a stage with no group (`post_log`) is `CellState.PENDING` with `""`. Keep the mapping as one module-level dict so MJ-30 deletes it. `build_view` sums with plain `sum`; `left = max(ceiling - cost, 0.0)`; `clips_to_review` is `snapshot.clips[ClipStatus.CHECKED]`. Import `montaj.board` at module level: `board_view` is only imported lazily from `dashboard.py` (Task 4), so there is no cycle.

- [ ] **Step 4: Run the tests and see them pass.** Run: `uv run pytest tests/test_board_view.py -v`. Expected: PASS.

- [ ] **Step 5: Commit.** `git add src/montaj/board_view.py tests/test_board_view.py && git commit -m "Add board view model with the 11-stage list"`

---

### Task 3: Recent outputs query

**Files:**
- Modify: `src/montaj/board_view.py`
- Test: `tests/test_board_view.py`

**Interfaces:**
- Consumes: `Settings`, `connect`, `Clip` (`hook_text`, `hook_a`, `raw_path`, `final_path`, `start_s`, `end_s`, `status`, `check_result`, `decision_reason`), `source_dir`'s data-dir convention: clip paths are relative to `settings.data_dir` (for example `clips/4/final.mp4`).
- Produces: `OutputRecord(clip_id: int, source_id: int, title: str, file: str, duration_s: float, status: ClipStatus, check_failed: bool, check_reason: str, finished_at: datetime | None)` and `recent_outputs(settings: Settings, limit: int = 5) -> tuple[OutputRecord, ...]`.

- [ ] **Step 1: Write the failing tests.** Use `_add_source`, plus clips inserted with `montaj.db.insert` (`Clip(id=..., source_id=7, start_s=0, end_s=42, status=...)`). Tests:
  - `test_recent_outputs_newest_first_and_skips_unrendered`: clips 1 (`CANDIDATE`), 2 (`RANKED`), 3 (`REJECTED`), 4 (`CHECKED`), 5 (`NEEDS_FIX`), 6 (`APPROVED`) give ids `[6, 5, 4]`; `limit=2` gives `[6, 5]`.
  - `test_record_fields`: a `CHECKED` clip with `hook_text="Why the first three seconds matter"`, `final_path="clips/4/final.mp4"` and a real file at `data_dir/clips/4/final.mp4` gives `title` equal to the hook text, `file == "final.mp4"`, `duration_s == 42.0`, `check_failed is False`, `check_reason == ""` and a `finished_at` datetime.
  - `test_failed_check_reason_is_shortened`: a `NEEDS_FIX` clip with `check_result=CheckResult.FAIL` and `decision_reason` of 400 characters gives `check_failed is True` and `len(check_reason) <= 160`, ending in `"..."`.
  - `test_missing_title_media_and_outside_path`: a clip with no hook, no paths gives `title == "Clip 4"`, `file == ""`, `finished_at is None`; a clip whose `final_path` is `"../escape.mp4"` gives `finished_at is None`.
  - `test_empty_database`: no clips gives `()`.

- [ ] **Step 2: Run them and see them fail.** Run: `uv run pytest tests/test_board_view.py -k recent -v`. Expected: FAIL with `ImportError`/`AttributeError` for `recent_outputs`.

- [ ] **Step 3: Implement `recent_outputs`.** One read-only query: `SELECT * FROM clips WHERE status IN (...) ORDER BY id DESC LIMIT ?` over the eight rendered statuses (`rendered, captioned, drafted, checked, needs_fix, approved, exported, posted`), each row through `Clip.model_validate(dict(row))`. Title is `hook_text` or `hook_a`, stripped, else `f"Clip {id}"`. `check_failed` is `status is NEEDS_FIX or check_result is FAIL`; `check_reason` is `decision_reason` only when failed, cut to 157 characters plus `"..."` past 160. `finished_at` comes from the file at `settings.data_dir / (final_path or raw_path)` using the same guard as `dashboard._artifact` (inside `data_dir` after `resolve()`, regular file, not a symlink), else `None`. Do not escape anything: the renderer (MJ-25) escapes.

- [ ] **Step 4: Run the tests and see them pass.** Run: `uv run pytest tests/test_board_view.py -v`. Expected: PASS.

- [ ] **Step 5: Commit.** `git add src/montaj/board_view.py tests/test_board_view.py && git commit -m "Add recent outputs query for the board view"`

---

### Task 4: The old overview reads the view model

**Files:**
- Modify: `src/montaj/dashboard.py` (`_topbar_html`, `_overview`, `dashboard_response`)
- Test: `tests/test_dashboard_board.py`

**Interfaces:**
- Consumes: `build_view`, `needs_attention`, `awaiting_review`, `Summary` from Task 2.
- Produces: `_topbar_html(summary: Summary) -> str`; `_overview` takes the `Summary` as a new argument. Rendered HTML is unchanged except for Task 1's money text.

- [ ] **Step 1: Write the failing test** in `tests/test_dashboard_board.py`: `test_home_figures_come_from_one_definition`. Seed one permitted source with one `CHECKED` clip and one unpermitted source (id 8) with one `CHECKED` clip. Assert the page shows `<dt>Needs attention</dt><dd>1</dd>`, `<dt>Clips awaiting review</dt><dd>1</dd>`, and that the Workload panel (`class="work-item"` links) shows the same two counts, so the figures can no longer drift apart. (This pins the shared predicates; it passes before the change only if today's code already agrees, so run it first and treat a pass as a regression guard.)

- [ ] **Step 2: Run it.** Run: `uv run pytest tests/test_dashboard_board.py::test_home_figures_come_from_one_definition -v`. Expected: PASS. Today's code already agrees (the unpermitted source is "Needs attention", the permitted one is awaiting review), so this is a regression guard for Step 3, not a failing-first test.

- [ ] **Step 3: Rewire `dashboard.py`.** In `dashboard_response`, next to the existing lazy `from montaj.board import build_rows`, import `build_view`. After `ceilings` is known, call `build_view(board, source_ceiling=ceilings.per_source, run_ceiling=ceilings.per_run, run_spend=run_spend)` and pass its `summary` into `_overview`. In `_overview`, replace the local `attention`, `review` and `review_count` computations with `needs_attention` and `awaiting_review` (lazy import inside the function, because `board_view` imports `board`, which imports `dashboard`). `_topbar_html(summary)` prints spend with `format_usd(summary.spend)`, the run line only when `summary.run_spend is not None` using `summary.run_ceiling`, and the two counts from the summary. Keep `BoardContext.run_ceiling` and `source_ceiling`: other pages still use them.

- [ ] **Step 4: Run the full checks.** Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest`. Expected: all pass, no change to any page except cost text.

- [ ] **Step 5: Commit.** `git add src/montaj/dashboard.py tests/test_dashboard_board.py && git commit -m "Read overview figures from the board view model"`

---

## Self-review

- **Spec coverage (MJ-22):** view model with four figures (Task 2), card records with dollars left (Task 2), recent outputs (Task 3), 11-stage list and mapping (Task 2), money formatter and all six sites (Task 1), old pages call the view model (Task 4), tests per figure, dollars-left, outputs order and reason, formatter (Tasks 1-3). The card `chip` is replaced by `state` and `tone` (see Calls).
- **Type consistency:** `StageSegment`, `SourceCard`, `Summary`, `OutputRecord` and `BoardView` are defined once in Tasks 2 and 3, and Task 4 uses `Summary`, `build_view`, `needs_attention`, `awaiting_review` with the same signatures.
- **Proportion:** four tasks, test code only where an exact value matters.
