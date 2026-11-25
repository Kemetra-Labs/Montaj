# Board v2 source page and confirm pages Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The source page, the cost confirmation pages and the error pages use the board home's shell, header and components, so the dashboard looks like one product. Behaviour does not change.

**Architecture:** One page shell. `dashboard_parts.render_board_page` becomes the only shell, and `render_dashboard_page` and the side rail go. `dashboard_header.header_html` gains a `home` flag so the home keeps `<h1>Pipeline Control</h1>` while other pages name "Pipeline Control" as a link home and put their own `<h1>` in the content. `dashboard_detail.render_detail` builds the source's `SourceCard` with `board_view.build_view([row], …)` and reuses `badge` for the chip and `dashboard_cards.stage_bar_html` for the 11-stage bar. Per-stage Run, Retry and Rerun keep their exact links and forms.

**Tech Stack:** Python 3.12, server-rendered HTML strings, `dashboard.css`, pytest, ruff, mypy.

**Spec:** Jira [MJ-28](https://rahmaqanater.atlassian.net/browse/MJ-28), epic [MJ-21](https://rahmaqanater.atlassian.net/browse/MJ-21). `DESIGN.md` for components.

## Global Constraints

- No behaviour change. Every link, form `action`, method and hidden field on the source and confirm pages stays the same. Every write keeps the same-origin token.
- The jobs page keeps redirecting to the board (`303`, `Location: /dashboard` or the source page, as today).
- The budget line keeps its words: "Source spend $x of $y source ceiling".
- Read-only (`preview serve`, `operations=False`): no Run, Retry, Rerun, form or confirm link, as today.
- One `<h1>` per page. No state carried by colour alone. Colours from `DESIGN.md` tokens only. No inline styles (CSP `style-src 'self'`). Every dynamic string escaped.
- Done when `tests/test_dashboard_detail.py` and `tests/test_dashboard_jobs.py` pass with only markup assertions updated, plus screenshots of the source page at 1366px and 375px.
- Before each commit: `.venv\Scripts\python.exe -m ruff check . ; ... ruff format --check . ; ... mypy src ; ... pytest` (`uv run` hangs here). Known on a clean `main`: 3 mypy import errors (cv2, numpy) and two environmental test failures (`test_engine::test_recipe_tracks_vision_availability_niche_and_loudness`, `test_speech::test_limits_download_errors_and_permission_basis`).
- Commits end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. One PR; the body says "MJ-28".

## Calls made while writing this (the owner can overrule)

- **The side rail goes from every page.** The home dropped it in MJ-23. Its two links stay reachable: "Pipeline Control" in the header goes to the board, and the source page keeps its "Preview clips ↗" button. The error pages (404, 503) move to the same shell.
- **Header on other pages:** brand, divider, then "Pipeline Control" as a link to `/dashboard` (not a heading), and the Live indicator while a job runs. The page's own `<h1>` is the source title or "Confirm cost". The "← Source queue" back link becomes "← Pipeline Control".
- **Source page layout, top to bottom:** header; back link; a source card in the board's card style (title as `<h1>`, `#id · niche · Campaign: …`, the chip, the 11-stage bar, then "Stage N of 11 · stage · free or paid stage" and "Source spend $x of $y source ceiling · $z left", and a Refresh link); the attention notice; the live job panel; then the board's two-column grid: Stages on the left; Source information, Clips and Saved checkpoints on the right.
- **Chip:** the same rules as the home card (`chip_for`), so "Blocked", "Running", "Over budget", "Ready for review" and so on read the same in both places. The source page does not read lock files, so it never shows "Locked"; that stays a board-home state. The over-budget chip and note are words only. The page's paid Run and Retry buttons stay as they are, and the server keeps refusing over-budget calls as today. Disabling them would be a behaviour change, out of this ticket.
- **Stage lines** keep their seven groups, labels, state words, notes and actions. Each line gains the board's segment swatch next to its state word, so it matches the bar.
- **Live job panel on the source page:** its problem text goes through `board_view.job_problem_note`, the same wording rule as the home's failed panel, so paths, URLs, markup and traces no longer reach this page either. MJ-27 left this for this ticket. It changes two assertions in `test_dashboard_detail.py` that pin the raw text; everything else in the panel keeps its logic.
- **Confirm pages:** the same header and back link to the source; one panel with the heading, the estimate lines, then "Confirm and run" (primary) and "Cancel" side by side. Forms unchanged.

## Review Focus

1. Every form and confirm link on the source and confirm pages carries exactly the fields and `href` it did before: token, source, stage, retry, confirm, resume, top. Task 1 pins it with a characterization test written before any markup moves.
2. The read-only source page shows the new layout with no Run, Retry, Rerun, form or confirm link. Task 2 pins it.
3. Every dashboard page (home, source, confirm, estimate unavailable, 404, 503) has exactly one `<h1>` and no rail. Task 1 pins it.
4. A long unbroken source title, campaign or permission basis does not cause sideways scroll at 375px. Task 3 checks it in the browser.
5. A live-job problem holding a path or markup does not reach the source page. Task 2 pins it.

---

## File structure

- Modify `src/montaj/dashboard_parts.py` (one shell), `src/montaj/dashboard_header.py` (`home` flag), `src/montaj/dashboard_detail.py` (source and confirm pages), `src/montaj/dashboard.py` (404, 503 and home call sites), `src/montaj/dashboard.css` (drop rail and frame rules, add source-page rules), `DESIGN.md`.
- Tests: `tests/test_dashboard_detail.py`, `tests/test_dashboard_header.py`, `tests/test_dashboard_jobs.py` (markup assertions only), and a new `tests/test_dashboard_forms.py` for the characterization test.

---

### Task 1: One shell for every page

**Files:** Modify `src/montaj/dashboard_parts.py`, `src/montaj/dashboard_header.py`, `src/montaj/dashboard_detail.py` (confirm pages and the wrapper call only), `src/montaj/dashboard.py`, `src/montaj/dashboard.css`; Test `tests/test_dashboard_forms.py` (create), `tests/test_dashboard_header.py`.

**Interfaces:**
- Produces: `header_html(live: bool, home: bool = True) -> str`. With `home=True`, `<h1>Pipeline Control</h1>` as today. With `home=False`, `<a class="page-name" href="/dashboard">Pipeline Control</a>` and no `<h1>`. `render_board_page(title, content, refresh=False)` is the only shell; `render_dashboard_page` is deleted.
- Confirm pages: `_confirm_page` and `render_confirm_unavailable` render `header_html(False, home=False)`, a back link `← Source #N`, `<h1>Confirm cost</h1>`, then one `<section class="panel confirm">` holding the heading, `<ul class="estimate">`, and `<div class="confirm-actions">` with the unchanged form and the Cancel link.

- [ ] **Step 1: Write the characterization test first, against the current code.** `tests/test_dashboard_forms.py::test_forms_and_confirm_links_are_unchanged` collects, on the source page (a source with a pending paid stage, a pending free stage, a failed stage and a done stage), the stage confirm page, the stage confirm page with `retry=yes`, and the batch confirm page, every `<form …>` `action` and `method` with its sorted `(name, value)` hidden inputs, and every `href` that contains `/confirm`. It compares them with literal expected lists. Run it: Expected PASS on the current code (it pins today's behaviour).
- [ ] **Step 2: Write the failing shell tests.**
  - `test_every_page_has_one_h1_and_no_rail` (Review Focus 3): home, source page, stage confirm, estimate unavailable, 404 and 503 (`monkeypatch` `build_rows` to raise `sqlite3.Error`) each have `page.count("<h1") == 1`, `'class="page-head"'`, and no `'class="rail"'`.
  - `test_other_pages_link_home_in_the_header`: the source page header holds `<a class="page-name" href="/dashboard">Pipeline Control</a>` and no `<h1>` inside `<header class="page-head">`.
  - Replace `test_other_pages_keep_the_rail` with the two tests above.
- [ ] **Step 3: Run them and watch them fail.** Run: `.venv\Scripts\python.exe -m pytest tests/test_dashboard_header.py tests/test_dashboard_forms.py -q`. Expected: the new header tests FAIL, the characterization test passes.
- [ ] **Step 4: Implement.** Switch every `render_dashboard_page` caller to `render_board_page` with `header_html(…, home=False)` in front of its content, delete `render_dashboard_page`, and give the 404 and 503 pages the same header. Delete `.shell`, `.rail` (and its media rules) and `.frame` from the CSS after `Grep` shows no other user. Add `.page-name`, `.confirm` and `.confirm-actions` (flex, wrap, gap).
- [ ] **Step 5: Run.** The header, form, detail and jobs tests. Expected: PASS; only markup assertions in `test_dashboard_detail.py` may need updating (the back link text), and the characterization test passes unchanged.
- [ ] **Step 6: Gate and commit:** `Put every dashboard page in the board shell`.

---

### Task 2: The source page in the board components

**Files:** Modify `src/montaj/dashboard_detail.py`, `src/montaj/dashboard.css`; Test `tests/test_dashboard_detail.py`.

**Interfaces:**
- Consumes: Task 1's shell and header; `board_view.build_view`, `SourceCard`; `dashboard_cards.stage_bar_html`; `dashboard_parts.badge`; `board_view.job_problem_note`. Import `board_view` and `dashboard_cards` inside `render_detail`, because `board` imports `dashboard`, which imports this module.
- Produces: `render_detail(row, ctx)` renders, in order: the header; `<a class="back" href="/dashboard">← Pipeline Control</a>`; `<section class="card source-card" aria-label="Source">` with `<h1>`, the eyebrow, the chip badge, `stage_bar_html(card)`, a `bar-meta` line with "Stage N of 11 · <stage> · free or paid stage" and "Source spend $x of $y source ceiling · $z left", and a Refresh link to the source page; the notice; `live_panel_html(ctx.live, source_id)`; `<div class="pipeline detail">` with the Stages panel and an `<aside class="side">` holding Source information, Clips and Saved checkpoints. Each stage line puts `<span class="seg {word}" aria-hidden="true"></span>` before its state word. `live_panel_html` shows `job_problem_note(live.problem)`.

- [ ] **Step 1: Write the failing tests** in `tests/test_dashboard_detail.py`:
  - `test_source_page_uses_the_card_chip_and_stage_bar`: the page has `class="card source-card"`, exactly one `<h1>` holding the title, the chip label the home card shows for the same source, `class="stage-bar"`, and, with one API call of $0.012345 logged for the source, "Source spend $0.01 of $2.00 source ceiling".
  - `test_source_page_columns`: `class="pipeline detail"`, and "Stages" comes before `<aside class="side"`, which holds "Source information", "Clips" and "Saved checkpoints" in that order.
  - `test_stage_lines_carry_a_segment_swatch`: each stage line has `class="seg ` and still has its state word.
  - `test_read_only_source_page_has_the_layout_and_no_controls` (Review Focus 2): `operations=False` gives `source-card` and `stage-bar` and no `<form`, `/confirm` or `<button`.
  - `test_live_panel_words_a_raw_problem` (Review Focus 5): a finished `LiveJob` for source 7 whose problem is `"<i>C:\\x\\y.mp4</i>"` shows "The job stopped. The details are in montaj.log" and no `y.mp4`.
  - Update `test_detail_shows_live_job_panel` and `test_over_budget_stop_is_shown_on_detail` to the worded problem ("The run ceiling was reached before the next paid call." for the budget stop; the raw `<i>` lock text becomes the generic sentence). Keep their other assertions.
- [ ] **Step 2: Run and watch them fail.** Run: `.venv\Scripts\python.exe -m pytest tests/test_dashboard_detail.py -q`. Expected: the new tests FAIL.
- [ ] **Step 3: Implement.** CSS, tokens only: `.source-card` (the `.card` look, one column, no thumbnail), `.source-card h1` (the size `.page-head h1` uses), `.pipeline.detail` (the home's grid ratio), `.stage-info .seg` (a small swatch inline with the state word).
- [ ] **Step 4: Run** `tests/test_dashboard_detail.py`, `tests/test_dashboard_jobs.py`, `tests/test_dashboard_forms.py`. Expected: PASS, with the characterization test unchanged (Review Focus 1).
- [ ] **Step 5: Gate and commit:** `Show the source page in the board components`.

---

### Task 3: Check and document

**Files:** Modify `src/montaj/dashboard.css` (fixes only), `DESIGN.md`.

- [ ] **Step 1: Visual check.** On a scratch database (never the real `data/`), with a source that has a long unbroken title, a long campaign name and a long permission basis, run the board (`.venv\Scripts\python.exe -c "from montaj.cli import app; app()" dashboard --port 8765` with `MONTAJ_DATA_DIR`). Screenshot at 1366px and 375px: the source page idle, the source page with a job pretended running (patch `JobQueue.running`), a stage confirm page and the estimate-unavailable page, plus the read-only source page (`preview serve`). Expected: one header style across pages, the chip and the stage bar match the home card, disabled buttons look disabled, and `document.documentElement.scrollWidth` equals the viewport width at 375px (Review Focus 4). Fix what fails, and stop the servers.
- [ ] **Step 2: `DESIGN.md`.** Note under the layout section that every dashboard page uses the board shell and header with no side rail, and add a **Source page** bullet with the order from "Calls made".
- [ ] **Step 3: Gate and commit:** `Document the board shell on every page`.

Final step for the executor: the whole-branch review (Opus), then the PR body per the repo's conventions (what changed, the "Calls made" above, the checks run, "MJ-28").
