# Board v2 special states Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The board home shows four special states from real conditions: an empty board that teaches the next step, a failed job in fixed wording with a Retry that opens the cost confirmation, a locked source with its holder and lock age, and an over-budget source or run with paid buttons disabled and the reason given.

**Architecture:** Data stays out of the markup. `board_view.py` gains a lock reader (`source_locks`, the only new I/O, read-only), an over-budget flag and chip rules on `SourceCard`, and `job_problem_note` (fixed wording for a stored job problem). A new `src/montaj/dashboard_states.py` renders the failed-job panel and the empty state. `dashboard_cards.py` and `dashboard_controls.py` show the lock, over-budget and ceiling-reached text. `dashboard_response` reads the locks next to the recent outputs.

**Tech Stack:** Python 3.12, server-rendered HTML strings, `dashboard.css`, pytest, ruff, mypy.

**Spec:** Jira [MJ-27](https://rahmaqanater.atlassian.net/browse/MJ-27), epic [MJ-21](https://rahmaqanater.atlassian.net/browse/MJ-21). `DESIGN.md` States section.

## Global Constraints

- Failure text is fixed wording, never raw exception text or paths. A stored job problem that is not one of Montaj's own fixed sentences is replaced, not shown.
- No "Clear stale lock" control and no new write path: the board only reads `data/sources/<id>/.lock-holder`.
- No loading skeleton: the board is server-rendered with no fetched content (the ticket says so).
- A paid action never starts without its estimate and one confirm click: Retry opens the existing cost confirmation. Over budget or locked, paid buttons are disabled and say why.
- No state carried by colour alone; colours from `DESIGN.md` tokens only; every dynamic string escaped.
- Before each commit: `.venv\Scripts\python.exe -m ruff check . ; ... ruff format --check . ; ... mypy src ; ... pytest` (`uv run` hangs here). Known on a clean `main`: 3 mypy import errors (cv2, numpy) and two environmental test failures (`test_engine::test_recipe_tracks_vision_availability_niche_and_loudness`, `test_speech::test_limits_download_errors_and_permission_basis`).
- Commits end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. One PR; the body says "MJ-27".

## Calls made while writing this (the owner can overrule)

- **Locked** means `.lock-holder` exists for the source (`media.source_lock` writes `{"command", "started"}` there and deletes it on release). The board does not try the OS lock (that would be a write-side effect). The source of this dashboard's own running job is not shown as locked; it is Running. A holder file left by a stopped process shows as locked, which is the "stale lock" the epic names; the text says so and points to the terminal, since clearing it is a later ticket.
- **Lock text:** "Locked by `<command>`. This lock is N minutes old." ("less than a minute", "1 minute", "N minutes", "N hours"). An unreadable holder file reads "Locked. The lock age is unknown." Resume and Retry are disabled with "Locked by another command".
- **Over budget** is a source whose spend is at or above its source ceiling (ceiling above 0). The card says "Over its $2.00 source ceiling. Paid stages are off for this source." and Resume and Retry are disabled with that reason. A running job at or above its run ceiling adds "Run ceiling reached" to the meter in warning ink. The server keeps refusing over-budget calls as today; the board only says so earlier.
- **Chip order:** Blocked, Running, Locked (warning), Over budget (warning), then the MJ-24 rules.
- **Failed job:** shown in place of the "Latest job" panel when the latest job is not running and has a problem. It names the stage and the source title, gives `job_problem_note(problem)`, says "Saved work is kept. Finished stages are reused when you retry.", gives the run spend so far, and a Retry link to the source's batch confirmation (`/dashboard/sources/<id>/confirm?stage=batch`), which shows the estimate. "Retry cost" is that estimate: no number exists before the confirm page (MJ-24's call). No Retry when the source is unknown (a failed import), blocked, locked, over budget or busy; an import failure says "Start it again from New job."
- **Fixed job wording:** Montaj's own fixed sentences pass through (`dashboard_jobs` constants, `source_problem` sentences, the lock-busy sentence, "No more eligible candidates in the ranked batch."); a budget stop (`"…: per-run budget ceiling $…"` or per-source) becomes "The run ceiling was reached before the next paid call." or "The source ceiling was reached before the next paid call."; anything else becomes "The job stopped. See the dashboard terminal for the details."
- **Empty board:** "No runs yet", then "Start one with New job, or import a permitted video with `montaj ingest`." and a New job button linking to `#new-job`; the New job form opens by default when the board has no sources, so the button lands on an open form. Read only: "Import a permitted video with `montaj ingest`, then refresh." and no button. "No sources match this view" stays for an empty filter.

## Review Focus

1. A stored job problem holding a path, a stack trace or HTML never reaches the page. Task 1 pins it.
2. A locked or over-budget source has no enabled paid button, and the reason is in words. Task 2 pins it.
3. A corrupt, empty or future-dated `.lock-holder` still renders the board and the card. Task 1 pins it.
4. The source of this dashboard's own running job is not shown as locked. Task 1 pins it.
5. The empty board's New job button lands on an open form, and the read-only empty board has no button. Task 3 pins it.

---

## File structure

- Modify `src/montaj/board_view.py`: `LockInfo`, `source_locks`, `job_problem_note`, `SourceCard.lock` and `over_budget`, chip rules, `build_view(locks=...)`.
- Create `src/montaj/dashboard_states.py`: `failed_job_html`, `empty_board_html`, `lock_age_text`.
- Modify `src/montaj/dashboard_cards.py`, `src/montaj/dashboard_controls.py`, `src/montaj/dashboard_new_job.py` (open when empty), `src/montaj/dashboard.py`, `src/montaj/dashboard.css`, `DESIGN.md`.
- Tests: `tests/test_board_view.py`, `tests/test_board_outputs.py` or a new `tests/test_board_states.py`, create `tests/test_dashboard_states.py`, `tests/test_dashboard_cards.py`, `tests/test_dashboard_controls.py`, `tests/test_dashboard_board.py`.

---

### Task 1: State data

**Files:** Modify `src/montaj/board_view.py`; Test `tests/test_board_states.py` (create), `tests/test_board_view.py`.

**Interfaces:**
- Produces: `LockInfo(command: str, started: datetime | None)` (frozen, slots); `source_locks(settings, source_ids, running: tuple[int, str] | None) -> dict[int, LockInfo]` (skips the running source; a missing file gives no entry; an unreadable or malformed file gives `LockInfo("", None)`; reads only inside `data_dir`, never through a symlink); `job_problem_note(problem: str) -> str`; `SourceCard.lock: LockInfo | None`, `SourceCard.over_budget: bool`; `build_view(..., locks: dict[int, LockInfo] | None = None)`; chips `Chip("Locked", "warning")` and `Chip("Over budget", "warning")` in the order above.

- [ ] **Step 1: Write the failing tests.**
  - `test_source_locks_reads_the_holder`: a `.lock-holder` of `{"command": "pipeline", "started": "2026-11-25T10:00:00+00:00"}` gives `LockInfo("pipeline", <that time>)`.
  - `test_source_locks_skips_the_running_source` (Review Focus 4).
  - `test_source_locks_survives_bad_files` (Review Focus 3): empty file, not JSON, JSON without fields, a `started` that is not a date → `LockInfo("", None)` (or the command alone); no exception.
  - `test_job_problem_note` (parametrized, Review Focus 1): each `dashboard_jobs` constant and `source_problem` sentence passes through; `"No more eligible candidates in the ranked batch."` passes; `"rank: per-run budget ceiling $0.500000; estimated spend $0.49, next call $0.02."` → the run sentence; the per-source form → the source sentence; `"C:\\data\\sources\\7\\video.mp4 not found"`, `"Traceback (most recent call last)"`, `"<b>x</b>"`, `""` → the generic sentence (empty gives `""`).
  - In `tests/test_board_view.py`: `test_lock_and_budget_chips` — a locked row gives `Locked`; a row whose cost is at its ceiling gives `Over budget`; blocked still wins over both; running wins over locked and over budget.
- [ ] **Step 2: Run and watch them fail.** Expected: FAIL on missing names.
- [ ] **Step 3: Implement.** Constants for the pass-through sentences are imported from where they live (`dashboard_jobs`, `dashboard_parts.source_problem` outputs, `job`), not retyped, where that does not create an import cycle; where it would, keep a single list in `board_view` and add a test that each listed sentence still exists in its module.
- [ ] **Step 4: Run the tests. Gate. Commit:** `Read source locks and word job problems for the board`.

---

### Task 2: State markup

**Files:** Create `src/montaj/dashboard_states.py`; Modify `src/montaj/dashboard_cards.py`, `src/montaj/dashboard_controls.py`, `src/montaj/dashboard.css`; Test `tests/test_dashboard_states.py` (create), `tests/test_dashboard_cards.py`, `tests/test_dashboard_controls.py`.

**Interfaces:**
- Produces: `lock_age_text(started: datetime | None, now: datetime) -> str`; `failed_job_html(live: LiveJob, titles: dict[int, str], retry: str) -> str` (`retry` is the Retry markup or the reason it is not offered); `empty_board_html(operations: bool) -> str`. In cards: a locked card's notes start with the lock text, an over-budget card's with the budget text; `_action_html` returns a disabled button (`<button disabled class="button">Resume</button>` plus the reason, as `busy_html` does) for a locked or over-budget source's Resume or Retry. In controls: the running meter adds `<span class="secondary run-limit">Run ceiling reached</span>` when spent is at or above the ceiling.

- [ ] **Step 1: Write the failing tests.**
  - `test_lock_age_text`: 20 s → "This lock is less than a minute old."; 60 s → "1 minute"; 4 min → "4 minutes"; 3 h → "3 hours"; `None` → "The lock age is unknown."
  - `test_locked_card` (Review Focus 2): chip `Locked`, "Locked by pipeline. This lock is 4 minutes old.", Resume as `<button disabled` with "Locked by another command", no `/confirm`.
  - `test_over_budget_card` (Review Focus 2): chip `Over budget`, "Over its $2.00 source ceiling. Paid stages are off for this source.", Retry disabled with that reason, no `/confirm`; review and export links stay.
  - `test_failed_job_panel`: a `LiveJob(7, "rank", False, 30.0, 0.12, "Stopped during rank.", "<raw path C:\\x>")` names "rank" and the source title, shows the generic sentence and not the raw text, "Saved work is kept…", "Run spend $0.12", and the Retry link to `/dashboard/sources/7/confirm?stage=batch`.
  - `test_failed_import_panel`: `source_id=None` → "Start it again from New job." and no Retry link.
  - `test_running_meter_says_ceiling_reached`: spent 0.5 of 0.5 → "Run ceiling reached"; 0.4 of 0.5 → absent.
  - `test_empty_board_html`: operations → "No runs yet", the `montaj ingest` pointer and `href="#new-job"` button; read only → no button.
- [ ] **Step 2: Run and watch them fail.**
- [ ] **Step 3: Implement.** CSS, tokens only: `.failed-job` (panel with a warning left rule, words first), `.lock-note`, `.budget-note` (warning ink, no background), `.run-limit` (warning ink).
- [ ] **Step 4: Run the tests. Gate. Commit:** `Render the locked, over-budget, failed and empty states`.

---

### Task 3: Wire the states and check them

**Files:** Modify `src/montaj/dashboard.py`, `src/montaj/dashboard_new_job.py`, `DESIGN.md`; Test `tests/test_dashboard_board.py`.

**Interfaces:**
- Produces: `dashboard_response` reads `source_locks(settings, ids, running)` for the home next to `recent_outputs`, in its own guard (a failure gives no locks); `_overview` renders `failed_job_html` in place of the live panel when the latest job failed, `empty_board_html` when there are no sources, and passes an `open_form` flag so `new_job_html` opens the form when the board is empty.

- [ ] **Step 1: Write the failing tests.**
  - `test_home_shows_a_locked_source`: write a `.lock-holder` for source 7 → the card shows `Locked` and the age text.
  - `test_home_does_not_lock_the_running_source`: same file and `running=(7, "batch")` → `Running`, not `Locked`.
  - `test_home_shows_the_failed_job`: a failed `LiveJob` → the failed panel, not the "Latest job" panel; the raw problem text is absent.
  - `test_empty_board_opens_the_form` (Review Focus 5): no sources → "No runs yet", `href="#new-job"`, and `<details id="new-job" class="panel job-panel" open>`; read only → no button and no form.
  - Update the existing "No sources yet" assertions to "No runs yet".
- [ ] **Step 2: Run and watch them fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the tests and the whole suite.**
- [ ] **Step 5: `DESIGN.md`.** Update the States paragraph: what each state shows, that there is no loading skeleton on the server-rendered board, and that clearing a lock is not on the board.
- [ ] **Step 6: Visual check.** On a scratch database (never the real `data/`): a locked source (write a holder file), an over-budget source, a failed job (scratch server that patches `JobQueue.live_job` to return a failed `LiveJob`), a run at its ceiling (patch `run_spend`), and an empty board. Screenshot each at 1366px and 375px. Expected: every state has words; disabled buttons look disabled; no sideways scroll. Fix what fails; stop the servers.
- [ ] **Step 7: Gate. Commit:** `Show the special states on the board home`.

Final step for the executor: the whole-branch review (Opus), then the PR body per the repo's conventions (what changed, the "Calls made" above, the checks run, "MJ-27").
