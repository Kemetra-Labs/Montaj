# Board v2 header and summary strip Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The board home (`/dashboard`) gets the new page shell: a "Pipeline Control" header with a live indicator, a four-cell summary strip, and a two-column grid. The source queue, notice banner, live job panel and New job form keep working.

**Architecture:** A new `src/montaj/dashboard_header.py` renders the header and the strip from the MJ-22 `Summary`, with no queries. `dashboard_parts.py` gets a second page function, `render_board_page`, for a shell with no side rail; every other page keeps `render_dashboard_page` until MJ-28. `_overview` in `dashboard.py` assembles the pieces.

**Tech Stack:** Python 3.12, server-rendered HTML strings, `dashboard.css`, pytest, ruff, mypy.

**Spec:** Jira [MJ-23](https://rahmaqanater.atlassian.net/browse/MJ-23), part of epic [MJ-21](https://rahmaqanater.atlassian.net/browse/MJ-21), which fixes the look and the data sources. `DESIGN.md` fixes colour, type and shape. No spec file in the repo; the tickets are the spec.

## Global Constraints

- Every figure comes from `Summary` (MJ-22). No invented numbers, no arrows, no "vs prior 24h".
- No state carried by colour alone: every tone pairs with words.
- Colours, type and shape come from `DESIGN.md` and the tokens in `dashboard.css`; add no new colours.
- Only `/dashboard` changes shell. Source pages, confirm pages, 404 and 503 keep the rail.
- No change to `source_allowed`, the compliance check, human review, the same-origin token or the loopback bind. Tests make no real API calls.
- New dollar text is HTML-escaped (`escape(format_usd(x))`), because `format_usd` can return `<$0.001`.
- Before each commit: `.venv\Scripts\python.exe -m ruff check . ; ... ruff format --check . ; ... mypy src ; ... pytest` (the repo's `uv run` hangs on this machine). Known: 3 mypy import errors (cv2, numpy) and two environmental test failures (`test_engine::test_recipe_tracks_vision_availability_niche_and_loudness`, `test_speech::test_limits_download_errors_and_permission_basis`) exist on a clean `main`.
- Commit messages are short and imperative. One PR for MJ-23; the body says "MJ-23".

## Calls made while writing this (the owner can overrule)

- **No side rail on the home page.** The comp has none, and the header carries the brand, so two brands would show. Home uses `render_board_page`; the rail stays on every other page until MJ-28 and is deleted in MJ-29. Cost if wrong: home loses its "Clip preview" link until MJ-25 adds Open review links to outputs.
- **The old source table sits in the left ("Active runs") column** until MJ-24 replaces it, so Resume, Retry, Open review, Export and the filters keep working. Its heading stays "Source queue" until then. The right column holds a "Recent outputs" placeholder until MJ-25.
- **The Workload panel goes now.** Its two counts are in the strip, and the strip's labels link to the same filtered views. The Resources panel (disk free) moves under the placeholder until MJ-26 puts disk free in the controls bar.
- **The toolbar goes.** "New job" is still reachable through the collapsible New job panel; "Refresh" goes, since a running job refreshes the page itself.
- **Cost cells stay neutral.** "Cost up is warning" has no data without history, so cells 1 and 2 are muted. The first use of a warning cost is MJ-27's over-budget state.
- **Cell wording.** Cell 3 note: "Blocked or flagged" or "Nothing flagged". Cell 4 note: "Checked, from permitted sources" or "None checked yet". Cell 2: "Run spend / $0.50 / of $5.00 run ceiling" while a job runs, "Run ceiling / $5.00 / No job running" otherwise.
- **Grid proportions** are `minmax(0, 1fr) minmax(16rem, 22rem)`, one column below 60rem (the epic's figure). MJ-24 and MJ-25 may retune.
- **Screenshots** at laptop and 375px are taken by the executor and shown to the owner in the session. GitHub cannot take images from the CLI, so the owner drags them into the PR if wanted.
- **Page title** becomes "Pipeline Control · Montaj" (was "Operations").

## Review Focus

1. A source with permission missing but `checked` clips must not count in "Clips awaiting review" on the page (MJ-22 rule, now rendered). Task 1 pins it.
2. A tiny cost (under a tenth of a cent) must read `<$0.001` as text, escaped in the HTML, never `$0.00` and never a raw `<`. Task 1 pins it.
3. An empty database must render the strip (`$0.00`, `0 sources`, zero counts) and the page. Task 1 pins it.
4. The read-only server (`preview serve`) shows no New job panel and no Live indicator and nothing else changes. Task 2 pins it.
5. The page must not scroll sideways at 375px, and the strip must stay readable (2 by 2). Task 3 checks it by screenshot.

---

## File structure

- Create `src/montaj/dashboard_header.py`: `header_html`, `summary_html`.
- Modify `src/montaj/dashboard_parts.py`: factor the shared document head, add `render_board_page`.
- Modify `src/montaj/dashboard.py`: remove `_topbar_html`; `_overview` builds the new page.
- Modify `src/montaj/dashboard.css`: header, strip and grid rules; remove the rules only the old heading, toolbar, top bar and Workload used.
- Modify `DESIGN.md`: one bullet each for the page header and the summary strip.
- Modify `tests/test_dashboard_board.py`; create `tests/test_dashboard_header.py`.

---

### Task 1: Summary strip

**Files:**
- Create: `src/montaj/dashboard_header.py`
- Test: `tests/test_dashboard_header.py` (create)

**Interfaces:**
- Consumes: `montaj.board_view.Summary` (`spend`, `source_count`, `run_spend: float | None`, `run_ceiling`, `attention`, `review_clips`), `montaj.dashboard_parts.format_usd`, `BASE_PATH`.
- Produces: `summary_html(summary: Summary) -> str`, a `<dl class="summary panel" aria-label="Production summary">` of four `<div class="stat" data-tone="...">`, each `<dt>label</dt><dd class="figure">…</dd><dd class="note">…</dd>`. Labels: `Est. API spend`, `Run spend` or `Run ceiling`, `Needs attention`, `Clips awaiting review`. The last two labels are links to `/dashboard?view=attention` and `/dashboard?view=review`. `data-tone` is `neutral`, `neutral`, `warning` (attention above zero, else `neutral`), `review` (review clips above zero, else `neutral`).

- [ ] **Step 1: Write the failing tests** in `tests/test_dashboard_header.py`, using `_home` as in `tests/test_dashboard_board.py` (move it to `tests/dashboard_helpers.py` and import it in both files) and a `_strip(page)` helper that returns the text of the `<dl class="summary` element with tags removed and whitespace collapsed. Tests:
  - `test_strip_text_idle`: seed source 7 (permitted, one `CHECKED` clip), source 8 (`speech_status="failed"`), one api call of `0.012345` on source 7, as in `test_home_top_bar_counts_attention_review_and_spend`. Assert the text equals `Est. API spend $0.01 2 sources Run ceiling $5.00 No job running Needs attention 1 Blocked or flagged Clips awaiting review 1 Checked, from permitted sources`.
  - `test_strip_text_while_a_job_runs`: same seed, `running=(7, "render")`, `run_spend=(0.5, 5.0)`. Assert the text contains `Run spend $0.50 of $5.00 run ceiling` and not `Run ceiling`.
  - `test_strip_links_to_the_filtered_views`: the page contains `href="/dashboard?view=attention"` and `href="/dashboard?view=review"` inside the strip.
  - `test_strip_tones_follow_the_counts`: with attention and review counts above zero the strip has `data-tone="warning"` and `data-tone="review"`; with an empty database neither appears.
  - `test_strip_empty_database`: no sources. Text equals `Est. API spend $0.00 0 sources Run ceiling $5.00 No job running Needs attention 0 Nothing flagged Clips awaiting review 0 None checked yet`.
  - `test_strip_does_not_count_an_unpermitted_source_as_awaiting_review` (Review Focus 1): permitted source with no clips plus an unpermitted source (`source_allowed=False`, basis `None`) with one `CHECKED` clip. The strip shows `Clips awaiting review 0`.
  - `test_strip_escapes_a_tiny_cost` (Review Focus 2): api cost `0.0002`. The strip's raw HTML contains `&lt;$0.001` and no `<$0.001`.
  - `test_strip_singular_source`: one source reads `1 source`.

- [ ] **Step 2: Run them and watch them fail.** Run: `.venv\Scripts\python.exe -m pytest tests/test_dashboard_header.py -q`. Expected: FAIL (no `summary` markup on the page yet; the old page still has `class="topbar`). Note which tests fail for the right reason.

- [ ] **Step 3: Implement `summary_html` in `src/montaj/dashboard_header.py`** and, in the same step, switch `_overview` in `src/montaj/dashboard.py` to call it in place of `_topbar_html`, then delete `_topbar_html`. The labels and notes are exactly the strings in the tests. Escape every dollar string.

- [ ] **Step 4: Run the new tests, then the board tests.** Run: `.venv\Scripts\python.exe -m pytest tests/test_dashboard_header.py tests/test_dashboard_board.py -q`. Expected: the new tests pass. `test_home_top_bar_counts_attention_review_and_spend` and `test_home_figures_come_from_one_definition` fail on the old `<dt>…</dt><dd>` strings: update those assertions to the new strip (the Workload assertions stay until Task 3), keeping `"<dt>Run spend</dt>" in live` at `tests/test_dashboard_board.py:220` as is.

- [ ] **Step 5: Add the strip's CSS** to `src/montaj/dashboard.css`: `.summary` (4 equal columns, `gap: 1px`, `background: var(--stroke)`, `overflow: hidden`; each `.stat` has `background: var(--surface)`), `.stat dt` (muted, `.74rem`), `.figure` (`2rem`, weight 650, tracking `-.04em`, tabular numbers), `.note` (muted, `.76rem`), and `.stat[data-tone="warning"] .note` / `[data-tone="review"] .note` in the warning and review inks. Two columns at `max-width: 52rem`. The label links use `--ink` and the global focus ring. Remove `.topbar` rules.

- [ ] **Step 6: Full gate and commit.** Run the whole suite (with the two environmental deselects), ruff check, ruff format check, mypy. Expected: green apart from the known mypy lines. Commit: `Render the summary strip from the board view`.

---

### Task 2: Header and the board shell

**Files:**
- Modify: `src/montaj/dashboard_header.py`, `src/montaj/dashboard_parts.py`, `src/montaj/dashboard.py`
- Test: `tests/test_dashboard_header.py`, `tests/test_dashboard_board.py`

**Interfaces:**
- Consumes: Task 1 module; `BoardContext.running`.
- Produces: `header_html(live: bool) -> str` returning `<header class="page-head">` with the `M` mark (`aria-hidden`), a link "Montaj" to `/dashboard`, a divider (`aria-hidden`), `<h1>Pipeline Control</h1>`, and when `live` a `<p class="live"><span aria-hidden="true"></span>Live</p>`. `render_board_page(title: str, content: str, refresh: bool = False) -> bytes` in `dashboard_parts.py`: the same document head, skip link and `<main id="main">` as `render_dashboard_page`, inside `<div class="page">`, with no rail. Both page functions build from one private `_document(title, body, refresh)` so the head is written once.

- [ ] **Step 1: Write the failing tests.**
  - `test_home_header`: the page has exactly one `<h1>` and it reads `Pipeline Control`; it contains `Montaj`; it has no `class="rail"`; `Production board` and `Refresh` are absent; the title is `Pipeline Control · Montaj`.
  - `test_live_indicator_only_while_a_job_runs`: `Live` appears with `running=(7, "render")` and not without.
  - `test_read_only_home_has_no_new_job_panel_or_live_indicator` (Review Focus 4): `operations=False` page has no `id="new-job"` and no `class="live"`, and still has the strip.
  - `test_other_pages_keep_the_rail`: the source page (`/dashboard/sources/7`) and the 404 page still contain `class="rail"`.
  - Update `tests/test_dashboard_board.py:342,366,470`: `Production board` becomes `Pipeline Control`. Update `test_home_top_bar_counts_attention_review_and_spend`: drop the `href="#new-job">New job</a>` assertion (the toolbar link goes; the panel stays).

- [ ] **Step 2: Run and watch them fail.** Run: `.venv\Scripts\python.exe -m pytest tests/test_dashboard_header.py tests/test_dashboard_board.py -q`. Expected: the new header tests FAIL (the old heading and rail are still there).

- [ ] **Step 3: Implement** `header_html`, `_document` and `render_board_page`; in `_overview` remove the `heading`/`toolbar` markup and the `new_job` link variable, build the page with `render_board_page("Pipeline Control", …, refresh=ctx.running is not None)`, and order the content: header, notice banner, live job panel, New job form, strip, grid (Task 3 finishes the grid).

- [ ] **Step 4: Run the tests.** Expected: PASS.

- [ ] **Step 5: Header CSS** in `dashboard.css`: `.page` (the old `.frame` width and padding), `.page-head` (flex row, gap, bottom hairline), `.page-head h1` (`1.6rem`, weight 670, tracking `-.045em`, per `DESIGN.md`), the divider (1px by 1.6rem, `--stroke`), `.live` (right aligned, `--success` ink on `--success-bg`, `.3rem` radius, with a small dot; the word "Live" carries the meaning). Reuse `.brand-mark`. Remove `.heading`, `.toolbar` and `.heading:has(.toolbar)` only after `Grep` shows nothing else (the detail page, `dashboard_detail.py`) uses them.

- [ ] **Step 6: Gate and commit.** Same gate as Task 1. Commit: `Give the board home a Pipeline Control header and no rail`.

---

### Task 3: Page grid, cleanup and visual check

**Files:**
- Modify: `src/montaj/dashboard.py`, `src/montaj/dashboard.css`, `DESIGN.md`
- Test: `tests/test_dashboard_board.py`

**Interfaces:**
- Consumes: Tasks 1 and 2.
- Produces: in `_overview`, `<div class="pipeline">` with the source queue (`<section class="queue">`, unchanged contents and filters) on the left and `<aside class="side" aria-label="Recent outputs and resources">` on the right holding `<section class="panel recent">` (h2 `Recent outputs`, one muted line "Nothing to show yet.") and the Resources panel (`Data volume`, with the existing `OSError` fallback).

- [ ] **Step 1: Write the failing tests.**
  - `test_home_grid_and_placeholder`: page has `class="pipeline"`, the `Source queue` section with the three filter links, a `Recent outputs` heading, and `Data volume`; it has no `Workload` and no `work-item`. Delete the two Workload assertions in `test_home_figures_come_from_one_definition` (`Needs attention</span><strong>…` and `Clips awaiting review</span><strong>…`); the strip assertions there stay.
  - `test_home_keeps_the_notice_live_panel_and_new_job_form`: with `notice="Hello"`, a running job and operations on, the page still holds the notice text, the live panel and `id="new-job"`; the `Another job is running` disabled-button behaviour is unchanged (the existing `test_run_buttons_are_disabled_while_a_job_runs` keeps passing).

- [ ] **Step 2: Run and watch them fail.** Run: `.venv\Scripts\python.exe -m pytest tests/test_dashboard_board.py -q`. Expected: FAIL on `pipeline` and `Workload`.

- [ ] **Step 3: Implement the grid** in `_overview`: replace `.board` markup with `.pipeline`, delete the Workload `<section>` and its `signal` markup.

- [ ] **Step 4: Run the tests.** Expected: PASS.

- [ ] **Step 5: CSS.** Add `.pipeline` (`grid-template-columns: minmax(0, 1fr) minmax(16rem, 22rem)`, gap 1rem, `align-items: start`) with one column below `60rem`, and `.recent`. Remove `.work-item*`, `.board:not(.detail) > .side` and any `.signal` rule only the Workload used, after `Grep` shows nothing else uses them. Keep `.board` and `.board.detail` for the source page.

- [ ] **Step 6: `DESIGN.md`.** Under Components add two bullets: **Page header** (brand mark, divider, h1 `1.6rem`, Live indicator with text) and **Summary strip** (four cells in one panel, 1px dividers, label, `2rem` figure, one note; tone changes the note's ink only and always comes with words).

- [ ] **Step 7: Visual check.** Seed a demo database (three sources: one permitted with checked clips, one failing check, one unpermitted; a few api costs) into the scratchpad, never the real `data/`. Run it with `MONTAJ_DATA_DIR=<demo dir> .venv\Scripts\python.exe -c "from montaj.cli import app; app()" dashboard --port 8765` (`python -m montaj` does not work here). Open `http://127.0.0.1:8765/dashboard` in the browser pane; screenshot at laptop width and at 375px (`resize_window`, then reset to desktop). Expected at laptop: header with `Pipeline Control`, four-cell strip in one panel, queue left, placeholder and Resources right. Expected at 375px: single column, strip 2 by 2, no horizontal page scroll (`document.documentElement.scrollWidth <= innerWidth` through the pane's JS tool), focus ring visible on the strip's links and the filters. Fix what fails; stop the server.

- [ ] **Step 8: Full gate and commit.** Commit: `Lay out the board home as a two-column grid with a placeholder`.

Final step for the executor: the whole-branch review, then the PR body per the repo's conventions (what changed, the "Calls made" above, the checks run, "MJ-23").
