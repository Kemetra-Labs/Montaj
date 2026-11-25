# Board v2 controls bar Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A controls bar at the bottom of the board home: the "New job" button that opens the existing form, a read-only run ceiling meter, disk free on the data volume, and a Refresh link. The Resources side panel goes.

**Architecture:** A new `src/montaj/dashboard_controls.py` renders the bar from the MJ-22 `Summary`, the board context and a disk reading, with no I/O of its own. `_overview` in `dashboard.py` reads the disk (as it does today), moves the existing `new_job_html` `<details>` into the bar and drops the Resources panel. The form, its token and its ceiling confirmation are untouched.

**Tech Stack:** Python 3.12, server-rendered HTML strings, `dashboard.css`, pytest, ruff, mypy.

**Spec:** Jira [MJ-26](https://rahmaqanater.atlassian.net/browse/MJ-26), epic [MJ-21](https://rahmaqanater.atlassian.net/browse/MJ-21) (look: `[ New job | Run ceiling meter ($ used of $ · $ left) | Disk free ]`). `DESIGN.md` for buttons (New run is 2.75rem), colour and focus.

## Global Constraints

- No control that removes or raises the spend ceiling. The meter is read only. The New job form keeps its fields, its same-origin token and the checkbox accepting the run and source ceilings (`dashboard_new_job.py` does not change).
- Read-only board (`preview serve`, `operations=False`): no New job button; meter, disk and Refresh still show.
- Every figure is real: run spend and ceiling from `Summary`, disk from `housekeeping.disk_free`, the warning level from `Settings.min_free_mb`.
- No state carried by colour alone; colours from `DESIGN.md` tokens only. Dollar text through `escape(format_usd(x))`.
- Before each commit: `.venv\Scripts\python.exe -m ruff check . ; ... ruff format --check . ; ... mypy src ; ... pytest` (`uv run` hangs here). Known on a clean `main`: 3 mypy import errors (cv2, numpy) and two environmental test failures (`test_engine::test_recipe_tracks_vision_availability_niche_and_loudness`, `test_speech::test_limits_download_errors_and_permission_basis`).
- Commits end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. One PR; the body says "MJ-26".

## Calls made while writing this (the owner can overrule)

- **"New job" is the form's own `<summary>`, styled as the 2.75rem primary button**, and the `<details>` moves from the top of the page into the bar. Clicking it opens the existing form in place with no JavaScript. When a refused submission brings the page back with a notice, the form opens as today. The empty-state link `#new-job` still scrolls to it.
- **Meter:** while a job runs, a bar filled to `spent / ceiling` (capped at 100%, and 0% for a zero ceiling with nothing spent) plus "Run spend $0.12 of $0.50 · $0.38 left", with `role="meter"` and `aria-valuemin`, `aria-valuemax`, `aria-valuenow`. Idle: "Run ceiling $0.50" and "No job running", no bar.
- **Disk:** "12.3 GB free" from the data volume, "Disk space unavailable" when the read fails. Below `min_free_mb` (the threshold `montaj doctor` already warns at) the text turns warning and adds "Below the 1024 MB warning level", in words.
- **Refresh is a text link** to the current view (`/dashboard?view=<view>`), shown in every mode. MJ-23 removed the toolbar Refresh; the ticket brings it back here.
- **The Resources side panel goes**; disk free lives in the bar. The right column holds only Recent outputs.

## Review Focus

1. The read-only board shows no New job button and no form, while meter, disk and Refresh still show. Task 1 pins it.
2. A failing disk read shows "Disk space unavailable" and the board still renders. Task 2 pins it.
3. The meter never shows a width above 100% or a negative "left", and a zero ceiling does not divide by zero. Task 1 pins it.
4. The New job path still creates a job: the existing form and POST tests pass unchanged, and the form still asks to accept both ceilings. Task 2 pins it.
5. At 375px the bar stacks without sideways scroll, and the open form spans the bar's width. Task 2 checks it.

---

## File structure

- Create `src/montaj/dashboard_controls.py`: `controls_html`, `meter_html`, `disk_html`.
- Modify `src/montaj/dashboard.py` (`_overview`), `src/montaj/dashboard.css`, `DESIGN.md`.
- Tests: create `tests/test_dashboard_controls.py`; update `tests/test_dashboard_board.py` and `tests/test_dashboard_header.py` where they pin the old placement.

---

### Task 1: Controls renderer

**Files:** Create `src/montaj/dashboard_controls.py`; Test `tests/test_dashboard_controls.py`; Modify `src/montaj/dashboard.css`.

**Interfaces:**
- Consumes: `board_view.Summary` (`run_spend: float | None`, `run_ceiling`); `dashboard_parts.format_usd`, `BASE_PATH`.
- Produces: `meter_html(summary: Summary) -> str`; `disk_html(free_bytes: int | None, min_free_mb: float) -> str`; `controls_html(summary: Summary, new_job: str, view: str, free_bytes: int | None, min_free_mb: float) -> str`, a `<section class="controls panel" aria-label="Controls">` holding, in order, `new_job` (the `<details>` markup, empty on a read-only board), the meter, the disk line and `<a class="refresh" href="/dashboard?view={view}">Refresh</a>`.

- [ ] **Step 1: Write the failing tests** in `tests/test_dashboard_controls.py`, building `Summary` directly:
  - `test_idle_meter_shows_the_ceiling_only`: `run_spend=None, run_ceiling=0.5` gives "Run ceiling", "$0.50", "No job running", and no `role="meter"`.
  - `test_running_meter`: `run_spend=0.12, run_ceiling=0.5` gives "Run spend $0.12 of $0.50 · $0.38 left", `role="meter"`, `aria-valuenow="0.12"`, `aria-valuemax="0.5"`, and a fill `style="width: 24%"`.
  - `test_meter_bounds` (Review Focus 3): spend 0.7 of 0.5 gives `width: 100%` and "$0.00 left"; spend 0 of ceiling 0 gives `width: 0%`; spend 0.0002 gives `&lt;$0.001` and no raw `<$0.001`.
  - `test_disk_free`: `free_bytes=12.3 * 1024**3` gives "12.3 GB free"; `None` gives "Disk space unavailable"; 512 MB with `min_free_mb=1024` gives the warning class and "Below the 1024 MB warning level".
  - `test_read_only_controls_have_no_new_job` (Review Focus 1): `new_job=""` gives no `new-job`, no `<form`, and still the meter, disk and `Refresh`.
  - `test_refresh_keeps_the_view`: `view="attention"` links to `/dashboard?view=attention`.
  - `test_no_ceiling_control`: the bar has no `<input`, `<button` or `<form` of its own (the passed `new_job` string excluded).
- [ ] **Step 2: Run and watch them fail.** Run: `.venv\Scripts\python.exe -m pytest tests/test_dashboard_controls.py -q`. Expected: FAIL on the missing module.
- [ ] **Step 3: Implement** `dashboard_controls.py`. Width is `round(min(spent / ceiling, 1) * 100)` when `ceiling > 0`, else 0 when nothing is spent and 100 otherwise. Format `aria-value*` with `repr`-free plain floats (`f"{x:g}"`).
- [ ] **Step 4: CSS**, tokens only: `.controls` (grid: New job, meter, disk, refresh; wraps to one column below 52rem; `.controls .job-panel` loses its panel border and padding; `.controls .job-panel > summary` gets the `.button.primary` look at `min-height: 2.75rem`, `list-style: none`, and `::-webkit-details-marker { display: none }`; `.controls .job-panel[open]` spans the full row), `.meter` (`--rail` track, `--stroke` border, accent fill, `.3rem` radius), `.disk.warning` (warning ink, no background), `.refresh` link.
- [ ] **Step 5: Run, gate, commit.** Commit: `Render the board controls bar`.

---

### Task 2: Move the controls onto the page

**Files:** Modify `src/montaj/dashboard.py`, `src/montaj/dashboard.css`, `DESIGN.md`; Test `tests/test_dashboard_board.py`, `tests/test_dashboard_header.py`.

**Interfaces:**
- Consumes: Task 1.
- Produces: `_overview` renders, in order: header, notice, live panel, summary strip, the grid (Active runs, Recent outputs), then `controls_html(data.summary, new_job_html(ctx), view, free_bytes, settings.min_free_mb)`. `free_bytes` is `disk_free(settings.data_dir)`, or `None` on `OSError`. The Resources panel and the old `space` string are gone.

- [ ] **Step 1: Write the failing tests** in `tests/test_dashboard_board.py`:
  - `test_controls_bar_is_last_and_holds_new_job`: on an operations board, `class="controls panel"` comes after `class="pipeline"`, and `id="new-job"` sits inside the controls section.
  - `test_disk_read_failure_still_renders_the_board` (Review Focus 2): `monkeypatch.setattr("montaj.dashboard.disk_free", raise OSError)` gives 200 and "Disk space unavailable".
  - `test_running_board_shows_the_meter`: `running=(7, "render")`, `run_spend=(0.12, 0.5)` gives `role="meter"` and "Run spend $0.12 of $0.50 · $0.38 left" in the controls.
  - Update `test_home_grid_and_placeholder`: "Data volume" and the Resources panel are gone; the disk line is in the controls. Update `tests/test_dashboard_header.py::test_home_header` to assert no `Refresh` inside `<header class="page-head">` only (Refresh is back, in the bar).
- [ ] **Step 2: Run and watch them fail.** Run: `.venv\Scripts\python.exe -m pytest tests/test_dashboard_board.py tests/test_dashboard_header.py -q`. Expected: the new tests FAIL.
- [ ] **Step 3: Implement** the move in `_overview`. Run the existing New job tests unchanged (Review Focus 4): `test_dashboard_board.py` form and POST tests, `tests/test_dashboard_jobs.py`, `tests/test_dashboard_new_job*.py` if present. Expected: PASS without edits to their assertions.
- [ ] **Step 4: Remove the CSS** only the Resources panel used (`.resources …`), after `Grep` shows no other user (`.facts` is the source page's; keep it).
- [ ] **Step 5: `DESIGN.md`.** Add a **Controls bar** bullet (New job as the 2.75rem primary summary of the form, read-only run ceiling meter with words, disk free with the doctor warning level, Refresh link; no ceiling switch).
- [ ] **Step 6: Visual check.** On a scratch database (never the real `data/`), run the board (`.venv\Scripts\python.exe -c "from montaj.cli import app; app()" dashboard --port 8765`) and a scratch server that pretends a job runs (patch `JobQueue.running` and `JobQueue.run_spend`, as in MJ-24). Screenshot at 1366px and 375px, form closed and open. Expected: one bar at the bottom, New job looks like the primary button and opens the form across the bar, meter readable with words, no sideways scroll at 375px. Fix what fails; stop the servers.
- [ ] **Step 7: Gate and commit.** Commit: `Move New job, the run ceiling and disk into the controls bar`.

Final step for the executor: the whole-branch review (Opus), then the PR body per the repo's conventions (what changed, the "Calls made" above, the checks run, "MJ-26").
