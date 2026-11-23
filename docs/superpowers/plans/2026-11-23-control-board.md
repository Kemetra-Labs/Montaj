# Control Board Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One loopback dashboard screen that lists every source with stage progress, runs single stages and new jobs from it, and shows cost, blocked reasons and live job state.

**Architecture:** A pure board-state module turns saved checkpoints, clip statuses and the live activity into stage cells and a next action. A thin stage runner calls the existing stage functions. `JobQueue` gains a stage submission beside the batch one, and `dashboard.py` renders the table and detail pane from the board state. Everything stays server-rendered HTML with form posts.

**Tech Stack:** Python 3.12, standard-library HTTP server, pydantic, SQLite, pytest, ruff, mypy.

**Spec:** [docs/superpowers/specs/2026-11-23-control-board-design.md](../specs/2026-11-23-control-board-design.md). Product context: [PRODUCT.md](../../../PRODUCT.md).

## Global Constraints

- Bind to loopback only. Every write needs the same-origin check and `JobQueue.token`.
- The response CSP is `default-src 'none'; style-src 'self'; form-action 'self'`, so pages carry no scripts. Live refresh is `<meta http-equiv="refresh" content="3">` only while a job runs.
- One job at a time. A second submission raises `ValueError("A production job is already running.")`. There is no queue. Other Run buttons are disabled with a reason.
- Never skip `source_allowed`, the permission basis, the compliance check or human review. Approve and reject stay in the clip preview.
- Paid stages (transcribe, candidates and rank, draft and check) show an estimate and need an explicit confirm. The server enforces this, not only the page.
- Each stage function (`transcribe_source`, `find_candidates`, `rank_source`, `cut_source`, `caption_source`, `draft_source`, `check_source`) takes its own `source_lock`. Do not wrap them in another lock.
- Escape all stored text (titles, notes, errors) with `html.escape`. Show fixed guidance for source failures as `_problem` does today.
- Tests use fixture media and fakes only. They never call real APIs.
- Before each commit, `uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest` must pass. Commit messages are short and imperative. The PR description links the issue with `Closes #N`.

## Review Focus

- A source with `source_allowed` false shows no Run button anywhere and says why (Task 4 test).
- A paid-stage POST without `confirm=yes` starts no job and makes no API call (Task 3 test).
- An unknown stage name or a missing source id on the stage POST answers 400 with a message, not a traceback (Task 3 test).
- A source locked by another process ends the job with a message naming the holder, and the board shows it (Task 3 test).
- A title or error containing `<script>` is escaped in the strip, table and detail pane (Task 4 test).

## File Structure

- Create `src/montaj/board.py`: pure board state (stage cells, next action, blocked reasons).
- Create `src/montaj/stage_jobs.py`: runs one board stage over the existing stage functions.
- Modify `src/montaj/dashboard_jobs.py`: stage submission, activity kind, estimates.
- Modify `src/montaj/dashboard.py`: render home table, top bar, detail pane, confirm page.
- Modify `src/montaj/preview.py`: stage routes, jobs path serves the board.
- Modify `src/montaj/dashboard.css`: strip, cells, disabled-action styles.
- Create `tests/test_board.py`, `tests/test_stage_jobs.py`. Modify `tests/test_dashboard.py`.
- Modify `docs/server.md`, `docs/ARCHITECTURE.md`.

---

### Task 1: Board state

**Files:**
- Create: `src/montaj/board.py`
- Test: `tests/test_board.py`

**Interfaces:**
- Consumes: `SourceSnapshot` and `_load(settings, source_id) -> list[SourceSnapshot]` from `montaj.dashboard`.
- Produces:
  - `class CellState(StrEnum)`: `DONE`, `RUNNING`, `BLOCKED`, `FAILED`, `PENDING` (values are the lowercase names).
  - `@dataclass(frozen=True, slots=True) class StageCell`: `key: str`, `label: str`, `state: CellState`, `reason: str = ""`.
  - `@dataclass(frozen=True, slots=True) class NextAction`: `kind: Literal["resume", "review", "export", "fix"]`, `label: str`.
  - `@dataclass(frozen=True, slots=True) class BoardRow`: `snapshot: SourceSnapshot`, `cells: tuple[StageCell, ...]`, `action: NextAction | None`.
  - `STRIP: tuple[tuple[str, str], ...]` of (key, label): `ingest`, `transcribe`, `rank` ("Candidates + rank"), `render` ("Cut + captions"), `draft` ("Draft + check"), `review`, `export`.
  - `build_rows(settings: Settings, *, running: tuple[int, str] | None, source_id: int | None = None) -> list[BoardRow]`. `running` is (source id, strip key) of the live job, or `None`.

- [ ] **Step 1: Write failing tests** in `tests/test_board.py`, using the `db_path` fixture and `Settings(data_dir=db_path.parent)`:
  - `test_new_source_has_only_ingest_pending`: a source with no files gives `ingest` PENDING and later cells BLOCKED with reason "Ingest first".
  - `test_saved_checkpoints_mark_transcribe_and_rank_done`: write `transcript.json`, `candidates.json`, `rank.json` into the source dir; `transcribe` and `rank` are DONE.
  - `test_unpermitted_source_blocks_everything`: `source_allowed=False` gives every cell BLOCKED with reason "Record permission for this source first." and `action is None`.
  - `test_running_stage_is_marked_running`: `running=(1, "render")` makes that cell RUNNING.
  - `test_checked_clips_offer_review_action`: a CHECKED clip gives `NextAction("review", "Open review")`; an APPROVED clip gives `NextAction("export", "Export")`; a NEEDS_FIX clip gives `NextAction("fix", ...)` and `draft` FAILED.
  - `test_speech_failure_marks_transcribe_failed_with_fixed_wording`: `speech_status="failed"` gives FAILED with the `_problem` text and no raw note.
- [ ] **Step 2: Run** `uv run pytest tests/test_board.py -v`. Expected: FAIL, module not found.
- [ ] **Step 3: Implement `board.py`.** `board.py` imports `SourceSnapshot` and `_load` from `montaj.dashboard`, so `dashboard.py` must import `build_rows` lazily inside the functions that use it (a module-level import would be circular). Derive cells from `SourceSnapshot.checkpoints`, `media`, `problem`, `clips` counters and `allowed`. The builder decides the exact rule for the clip-stage cells (`render`, `draft`, `review`, `export`) from `ClipStatus` counts, as long as the six tests hold and a cell is DONE only when at least one clip has reached its output status. Action priority: fix, review, export, resume. `resume` label is "Resume" when any non-blocked cell is PENDING.
- [ ] **Step 4: Run** the same command. Expected: PASS.
- [ ] **Step 5: Commit** `git add src/montaj/board.py tests/test_board.py && git commit -m "Add board state for source stages"`.

---

### Task 2: Stage runner

**Files:**
- Create: `src/montaj/stage_jobs.py`
- Test: `tests/test_stage_jobs.py`

**Interfaces:**
- Consumes: the stage functions listed in Global Constraints, `Budget`, `Settings`, `RankSet`, `CandidateBounds`.
- Produces:
  - `class BoardStage(StrEnum)`: `TRANSCRIBE="transcribe"`, `RANK="rank"` (candidates then rank), `RENDER="render"` (cut then captions), `DRAFT="draft"` (draft then check).
  - `PAID_STAGES: frozenset[BoardStage]` of `TRANSCRIBE`, `RANK`, `DRAFT`.
  - `@dataclass(frozen=True, slots=True) class StageResult`: `summary: str`, `problem: str = ""`.
  - `run_board_stage(settings: Settings, source_id: int, stage: BoardStage, *, top: int, budget: Budget, progress: Callable[[str], None], retry_failed: bool = False, backend: Transcriber | None = None, llm: LLM | None = None, renderer: Renderer | None = None) -> StageResult`.

- [ ] **Step 1: Write failing tests** in `tests/test_stage_jobs.py`, patching the stage functions in `montaj.stage_jobs` with recorders:
  - `test_rank_stage_runs_candidates_then_rank_in_order`.
  - `test_render_stage_touches_only_selected_clips_up_to_top`: `rank.json` selects clips 1 to 5, `top=2`, only clips 1 and 2 are cut and captioned.
  - `test_render_stage_requires_a_saved_ranking`: no `rank.json` raises `ValueError("Run rank first.")`.
  - `test_retry_failed_only_touches_needs_fix_clips`.
  - `test_approved_clips_are_never_rerun`: an APPROVED clip in the selection is skipped.
  - `test_draft_stage_runs_draft_then_check_and_reports_failures`: a FAIL check outcome appears in `summary`.
  - `test_budget_exceeded_returns_problem_not_exception`: `BudgetExceeded` yields `StageResult(problem=<message>)`.
- [ ] **Step 2: Run** `uv run pytest tests/test_stage_jobs.py -v`. Expected: FAIL, module not found.
- [ ] **Step 3: Implement `run_board_stage`.** Selected clip ids come from the saved `rank.json` (`RankSet.selected_ids`), never from a fresh `rank_source` call, because that could spend. Reuse `job.has_review_decision` handling for approved clips. Call `progress("...")` before each step with the same wording style as `produce_batch`.
- [ ] **Step 4: Run** the same command. Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "Run single pipeline stages from the board"` with both files added.

---

### Task 3: Stage submission, estimates and routes

**Files:**
- Modify: `src/montaj/dashboard_jobs.py` (`Activity`, `JobQueue`), `src/montaj/preview.py` (`_handler.do_POST`, `_post_action`)
- Test: `tests/test_dashboard.py`

**Interfaces:**
- Consumes: `run_board_stage`, `BoardStage`, `PAID_STAGES`, `StageResult`; `run.estimate_run(settings, source_id, until, language, budget) -> list[str]`.
- Produces:
  - `class StageSubmission(BaseModel)`: `source: int` (gt 0), `stage: BoardStage`, `top: int = DEFAULT_TARGET`, `budget: float | None`, `confirm: bool = False`, `retry: bool = False`.
  - `Activity` gains `kind: Literal["batch", "stage"] = "batch"` and `stage_key: str = ""`.
  - `JobQueue.submit_stage(submission: StageSubmission) -> None` raising `ValueError` for: a running job, a paid stage without `confirm`, an unpermitted source, an unknown source.
  - `JobQueue.estimate(source_id: int, stage: BoardStage, budget: float | None) -> list[str]` returning `estimate_run` lines for the paid stage's `until` value (`transcribe`, `rank`, `draft`).
  - `JobQueue.running() -> tuple[int, str] | None`, feeding `build_rows`: `(source id, strip key)` while a job runs, `None` otherwise. A batch job returns key `"batch"` (the board marks that source's first PENDING runnable cell RUNNING). While a new source is still importing the source id is `0`: no row is RUNNING, every Run button is disabled with "Another job is running", and the text omits the source number.
  - POST `/dashboard/stage` (added to the POST allowlist) redirecting to `/dashboard/sources/{id}`.

- [ ] **Step 1: Write failing tests** following `test_dashboard_job_progress_is_per_server_and_resume_uses_source_id` (monkeypatch `montaj.dashboard_jobs.Thread` and `montaj.dashboard_jobs.run_board_stage`):
  - `test_stage_submit_runs_one_stage_and_blocks_a_second_job`: a second `submit_stage` raises "already running".
  - `test_paid_stage_without_confirm_starts_nothing`: `ValueError` mentions confirming the cost, the fake runner is never called, `snapshot()` is `None`.
  - `test_free_stage_runs_without_confirm`: `StageSubmission(stage=RENDER)` starts.
  - `test_stage_on_unpermitted_source_is_refused`.
  - `test_locked_source_reports_holder_in_activity_problem`: the fake raises `MediaError("... held by cut ...")`; activity ends not running with that text in `problem`.
  - `test_stage_post_requires_token_and_same_origin`: parametrize like the existing job POST test; valid gives 303 to `/dashboard/sources/7`.
  - `test_stage_post_rejects_unknown_stage_and_missing_source`: status 400 and body contains the message.
- [ ] **Step 2: Run** `uv run pytest tests/test_dashboard.py -k "stage" -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the interfaces. `submit_stage` takes `self._lock` and starts a daemon `Thread` exactly as `submit` does, with a `_work_stage` mirroring `_work`. Wire `estimate` and `running`. Add the route to `do_POST` and `_post_action`; on `ValueError` respond 400 with the message page used for review errors.
- [ ] **Step 4: Run** `uv run pytest tests/test_dashboard.py -v`. Expected: PASS (existing tests unchanged).
- [ ] **Step 5: Commit** `git commit -m "Submit single-stage jobs from the dashboard"`.

---

### Task 4: Home table and top bar

**Files:**
- Modify: `src/montaj/dashboard.py` (`_overview`, `dashboard_response`), `src/montaj/dashboard.css`
- Test: `tests/test_dashboard.py`

**Interfaces:**
- Consumes: `build_rows`, `BoardRow`, `CellState`, `JobQueue.running()` and `JobQueue.token`.
- Produces: `dashboard_response(settings, path, view, *, operations: bool = False, running: tuple[int, str] | None = None, token: str = "", notice: str = "")` (new keyword arguments default so existing callers keep working). `handle_preview` passes `jobs.running()` and `jobs.token`.

- [ ] **Step 1: Write failing tests:**
  - `test_home_rows_show_stage_strip_and_next_action`: strip cells carry text labels (not colour alone) and the row has one primary button.
  - `test_home_top_bar_counts_attention_review_and_spend`: shows estimated spend for all sources and, while a job runs, its run spend against the run ceiling. There is no daily cap.
  - `test_unpermitted_source_row_has_no_run_buttons`: the row says "Record permission for this source first." and contains no `<form` or `<button`.
  - `test_run_buttons_are_disabled_while_a_job_runs`: `running=(2, "render")` disables every button with "Another job is running (source 2)".
  - `test_home_escapes_titles_and_errors`: a title and failure note containing `<script>` appear escaped.
  - `test_home_refreshes_only_while_running`: the meta refresh tag is present only when `running` is set.
  - `test_empty_board_points_to_new_job`.
- [ ] **Step 2: Run** `uv run pytest tests/test_dashboard.py -k "home" -v`. Expected: FAIL.
- [ ] **Step 3: Implement.** The primary button is a POST form to `/dashboard/stage` (kind `resume` posts the batch via `JOBS_PATH` with `resume=<id>`), a link to `/?source=<id>` for `review`, and the existing export flow for `export`. Keep the `all`, `attention` and `review` filters. Add CSS for the strip cells using the existing tokens; state is also a text label.
- [ ] **Step 4: Run** `uv run pytest tests/test_dashboard.py -v`. Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "Show stage progress and next actions on the board"`.

---

### Task 5: Detail pane and cost confirmation

**Files:**
- Modify: `src/montaj/dashboard.py` (`_detail`, new `render_confirm`), `src/montaj/preview.py` (`handle_preview`), `src/montaj/dashboard.css`
- Test: `tests/test_dashboard.py`

**Interfaces:**
- Consumes: `build_rows`, `JobQueue.estimate`, `PAID_STAGES`.
- Produces: `render_confirm(source_id: int, stage: BoardStage, lines: list[str], token: str) -> bytes` in `dashboard.py` (pure, no `JobQueue` import, which would be circular). `handle_preview` serves GET `/dashboard/sources/{id}/confirm?stage=<BoardStage>` by calling `jobs.estimate(...)` and `render_confirm`; it shows the estimate lines and a POST form to `/dashboard/stage` with `confirm=yes`. Unknown stage or source gives 404. `_detail` renders Run for PENDING cells, Retry for FAILED cells, and no button for DONE cells (Task 7 adds the explicit Rerun).

- [ ] **Step 1: Write failing tests:**
  - `test_detail_lists_every_stage_with_status_and_action`: one line per strip stage, Run or Retry where allowed, "why blocked" text otherwise.
  - `test_paid_stage_links_to_confirm_and_free_stage_posts_directly`.
  - `test_confirm_page_shows_estimate_lines_and_confirm_form`: contains the `estimate_run` lines (monkeypatch it) and the hidden `confirm` and `token` fields.
  - `test_detail_shows_live_job_panel`: stage name, elapsed time, run spend, and the problem text in fixed wording.
  - `test_detail_links_to_clip_preview`.
  - `test_confirm_unknown_stage_is_404`.
- [ ] **Step 2: Run** `uv run pytest tests/test_dashboard.py -k "detail or confirm" -v`. Expected: FAIL.
- [ ] **Step 3: Implement** by extending `_detail` with the stage list and live panel, and add the confirm route to `dashboard_response`. Keep the existing checkpoint and clip lists.
- [ ] **Step 4: Run** `uv run pytest tests/test_dashboard.py -v`. Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "Run stages from the source detail pane"`.

---

### Task 6: New job on the board, jobs page retired

**Files:**
- Modify: `src/montaj/dashboard.py`, `src/montaj/dashboard_jobs.py` (replace `JobQueue.page` with a `JobQueue.status_panel() -> str` fragment the board renders), `src/montaj/preview.py` (`handle_preview`, `do_POST` error path, nav link)
- Test: `tests/test_dashboard.py` (the two assertions that call `jobs.page()` at lines 214 and 219 move to the board response, asserting the same text: "2/2 clips ready" and "/?source=7&amp;ready=1")

**Interfaces:**
- Consumes: `Submission`, `dashboard_location`, `JOBS_PATH`.
- Produces: the board home renders a "New job" panel (a `<details>` form with the existing fields and the permission checkbox and basis). `GET /dashboard/jobs` returns the board home page. `POST /dashboard/jobs` still accepts `Submission`; on error it re-renders the board with the message as `notice`.

- [ ] **Step 1: Write failing tests:**
  - `test_board_has_new_job_form_with_permission_fields`: URL or filename, campaign, niche, basis, permission checkbox, target, optional budget, token.
  - `test_jobs_path_serves_the_board`.
  - `test_new_job_error_rerenders_board_with_notice`: a non-YouTube URL shows the validation message on the board.
  - `test_nav_no_longer_links_production_jobs`.
  - The existing job tests keep passing, with only the two `jobs.page()` assertions moved to the board response as noted under Files.
- [ ] **Step 2: Run** `uv run pytest tests/test_dashboard.py -k "job" -v`. Expected: FAIL for the new tests.
- [ ] **Step 3: Implement.** Move the form markup from `JobQueue.page` into a function in `dashboard.py`. Keep the "Resume a source" capability as the Resume row action. Remove the "Production jobs" button from the overview.
- [ ] **Step 4: Run** `uv run pytest -q`. Expected: all pass.
- [ ] **Step 5: Commit** `git commit -m "Start jobs from the board and retire the jobs page"`.

---

### Task 7: Remaining states and docs

**Files:**
- Modify: `src/montaj/board.py`, `src/montaj/dashboard.py`, `docs/server.md`, `docs/ARCHITECTURE.md`, `README.md` (line 226 still tells the operator to open **Production jobs**; rewrite it for the board)
- Test: `tests/test_board.py`, `tests/test_dashboard.py`

**Interfaces:**
- Consumes: Tasks 1 to 6.
- Produces: no new public names.

- [ ] **Step 1: Write failing tests:**
  - `test_restarted_dashboard_shows_checkpoints_and_resume`: a fresh `JobQueue` has `running() is None`; a source with saved checkpoints shows them and a Resume action.
  - `test_failed_check_blocks_review_export_with_reason`: `NEEDS_FIX` clips make `export` BLOCKED with "Check has failures. Fix the clips first."
  - `test_over_budget_stop_is_shown_on_detail`: an activity whose `problem` is the `BudgetExceeded` text appears in the live panel.
  - `test_up_to_date_stage_offers_explicit_rerun`: a DONE stage shows "Up to date" and a "Rerun" action, not a silent run.
- [ ] **Step 2: Run** `uv run pytest tests/test_board.py tests/test_dashboard.py -v`. Expected: FAIL for these tests.
- [ ] **Step 3: Implement** the states in `board.py` and `dashboard.py`. In `docs/server.md` replace "Running stages and making review decisions are terminal operations" with the new behavior (stages and new jobs run from the board; review decisions stay in the clip preview) and describe the one-job limit. In `docs/ARCHITECTURE.md` update the review stage line only if it contradicts.
- [ ] **Step 4: Run** `uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest`. Expected: all pass.
- [ ] **Step 5: Commit** `git commit -m "Show restart, failure and budget states on the board"`.
