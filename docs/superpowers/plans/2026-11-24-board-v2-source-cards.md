# Board v2 source cards Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the source table on the board home with the "Active runs" panel: one card per source with a state chip, an 11-segment stage bar, a budget line, a next-action sentence and one button, plus a legend. Filters stay in the panel head.

**Architecture:** The view model (`board_view.py`) gains the chip, the current stage and a free/paid flag, so the renderer only prints. A new `src/montaj/dashboard_cards.py` renders cards, the bar and the legend, and takes over the action and note helpers from `dashboard.py`. `_overview` swaps the table for the cards.

**Tech Stack:** Python 3.12, server-rendered HTML strings, `dashboard.css`, pytest, ruff, mypy.

**Spec:** Jira [MJ-24](https://rahmaqanater.atlassian.net/browse/MJ-24), part of epic [MJ-21](https://rahmaqanater.atlassian.net/browse/MJ-21) (look, data sources, guardrails). `DESIGN.md` fixes the stage bar, chip, next-action and button rules. The tickets are the spec; no spec file in the repo.

## Global Constraints

- A paid stage never starts without its estimate and one confirm click: Resume, Retry and paid stage Run keep opening the existing confirm pages. The card never posts a paid job.
- A source that is not permitted (`permission_reason` non-empty) never offers an action, paid or free, and says why.
- No state carried by colour alone: every chip, segment and legend entry has words (the bar's `aria-label` lists every stage and state).
- Real data only: every figure comes from `BoardView`/`BoardRow`. No invented numbers.
- New dollar text is HTML-escaped (`escape(format_usd(x))`).
- Colours, type and shape come from `DESIGN.md` and the tokens in `dashboard.css`; add no new colours.
- Same-origin token, loopback bind and read-only `preview serve` behaviour do not change. Tests make no real API calls.
- Before each commit: `.venv\Scripts\python.exe -m ruff check . ; ... ruff format --check . ; ... mypy src ; ... pytest` (`uv run` hangs here). Known on a clean `main`: 3 mypy import errors (cv2, numpy) and two environmental test failures (`test_engine::test_recipe_tracks_vision_availability_niche_and_loudness`, `test_speech::test_limits_download_errors_and_permission_basis`).
- Commit messages are short and imperative. One PR for MJ-24; the body says "MJ-24".

## Calls made while writing this (the owner can overrule)

- **No dollar estimate on the card.** An estimate only exists for transcription; `estimate_run` says rank and draft cost "depends on inputs and cache reuse", and computing it reads and hashes the audio on every page load and every 3 s refresh while a job runs. The card names the stage, says free or paid, shows the real source spend and what is left, and says the estimate is shown before anything is paid. The button still opens the confirm page that has it. Cost if wrong: the owner wanted numbers on the card, which needs a cached per-stage estimate (its own ticket).
- **"Stage N of 11"** is the first running stage, else the first stage that is not done. A finished source reads "Stage 11 of 11 · Post log" because Post log is waiting until MJ-30.
- **Paid stages** are `transcribe`, `rank`, `draft`, the three values in `stage_jobs.PAID_STAGES`. Candidates and Check are free.
- **"Next" segment** is the current stage when its state is pending; every other pending segment is "Waiting".
- **Chips.** The six in the ticket (Running, Needs your confirm, Blocked, Failed, Ready for review, Done) plus two for states they miss: "Ready to export" (approved clips, review tone) and "Waiting" (nothing to run yet, neutral). First match wins: blocked, running, failed or flagged clips, checked clips, approved clips, a Resume action, all stages done, else waiting.
- **Blocked cards show no review count.** MJ-22's note asked for this decision; `clips_to_review` becomes 0 for a source that is not permitted, matching the summary strip.
- **The Resources panel (disk free) and the Recent outputs placeholder stay** under the cards until MJ-26 and MJ-25. The ticket says to delete the Resources panel, but disk free has nowhere else to live before MJ-26; the Workload panel went in MJ-23.
- **Source cards link to the source page** from the title, as table rows did. Per-stage Run stays on the source page.
- **Thumbnail** is a placeholder block showing the duration text (nothing when unknown).

## Review Focus

1. A source with no permission or no recorded basis has no button, no form and no confirm link, even when stages are pending; the card says why. Task 2 pins it.
2. While a job runs, every paid action on the board is a disabled button with a reason, the running source's chip reads "Running" and its bar shows a running segment, and review and export links still work. Task 2 pins it.
3. A source over its ceiling shows `$0.00 left`, never a negative; a sub-cent cost reads `&lt;$0.001` in the HTML. Task 2 pins it.
4. A title containing HTML or a very long unbroken word is escaped and wraps without sideways page scroll. Task 2 pins the escaping; Task 3 checks the wrap at 375px.
5. An empty board and a filter with no matches still show their messages, and the three filters list the right sources. Task 3 pins it.

---

## File structure

- Modify `src/montaj/board_view.py`: `Chip`, `chip_for`, `current_stage`, `PAID_STAGE_KEYS`, `segment_word`; `SourceCard` gains `chip` and `current`; `clips_to_review` gated by permission.
- Create `src/montaj/dashboard_cards.py`: card, bar, legend, next-action markup; `_resume_html`, `_action_html`, `_note`, `_notes_html` move here from `dashboard.py`.
- Modify `src/montaj/dashboard.py`: `_overview` renders cards; delete `_table_html`, `_row_html`, `_cell_html`, the moved helpers.
- Modify `src/montaj/dashboard.css`, `DESIGN.md`.
- Tests: `tests/test_board_view.py`, create `tests/test_dashboard_cards.py`, update `tests/dashboard_helpers.py` (`_rows`) and the table-coupled tests in `tests/test_dashboard_board.py`.

---

### Task 1: View model: chip, current stage, paid flag

**Files:**
- Modify: `src/montaj/board_view.py`
- Test: `tests/test_board_view.py`

**Interfaces:**
- Consumes: MJ-22 `StageSegment`, `SourceCard`, `BoardView`, `build_view`, `awaiting_review`, `_cell_key`; `board.BoardRow`, `CellState`, `NextAction`.
- Produces: `Chip(label: str, tone: str)` (frozen, slots), tones `neutral | warning | review | success | running`; `chip_for(row: BoardRow, segments: tuple[StageSegment, ...]) -> Chip`; `current_stage(segments) -> int` (index; first running, else first not done, else the last index); `PAID_STAGE_KEYS: frozenset[str] = {"transcribe", "rank", "draft"}`; `segment_word(segment: StageSegment, is_current: bool) -> str` returning `done`, `running`, `blocked`, `failed`, `next` (pending and current) or `waiting`; `SourceCard.chip: Chip` and `SourceCard.current: int`.

- [ ] **Step 1: Write the failing tests** in `tests/test_board_view.py` (reuse its `_row`, `_cells`, `_snapshot` helpers; add `clips=` and `allowed=` overrides as needed):
  - `test_chip_for_each_state`: parametrized, one row per case, asserting `(label, tone)`: unpermitted source with checked clips gives `("Blocked", "warning")`; a running cell gives `("Running", "running")`; a failed cell gives `("Failed", "warning")`; a `fix` action gives `("Failed", "warning")`; a checked clip gives `("Ready for review", "review")`; an approved clip gives `("Ready to export", "review")`; a `resume` action on pending cells gives `("Needs your confirm", "review")`; every non-`post_log` segment done and no action gives `("Done", "success")`; pending cells and no action gives `("Waiting", "neutral")`. Add a case that blocked wins over running and running wins over failed.
  - `test_current_stage_and_segment_words`: with cells done, done, running, pending, blocked, blocked, blocked, `current_stage` is the running index (2 or 3 per the group mapping; assert the first running index); with none running it is the first not-done index; with everything done except `post_log` it is 10. `segment_word` gives `next` for the current pending segment, `waiting` for another pending one, and the plain state words otherwise.
  - `test_paid_stage_keys_match_the_board_stages`: `PAID_STAGE_KEYS == {stage.value for stage in stage_jobs.PAID_STAGES}`.
  - `test_blocked_card_shows_no_review_count` (MJ-22 note): unpermitted source with two checked clips has `card.clips_to_review == 0`; a permitted one with two has 2.

- [ ] **Step 2: Run and watch them fail.** Run: `.venv\Scripts\python.exe -m pytest tests/test_board_view.py -q`. Expected: FAIL on missing names.

- [ ] **Step 3: Implement** the names in `board_view.py`. `_card` calls `stage_segments(row.cells)` once and passes it to `chip_for` and `current_stage`. `chip_for` checks, in order, the rules in "Calls made". Gate `clips_to_review` with `awaiting_review(row)`.

- [ ] **Step 4: Run the tests, then the board tests.** Run: `.venv\Scripts\python.exe -m pytest tests/test_board_view.py tests/test_dashboard_board.py -q`. Expected: PASS.

- [ ] **Step 5: Gate and commit.** Run the whole suite (two environmental deselects), ruff check, ruff format check, mypy. Commit: `Add chips and the current stage to the board view`.

---

### Task 2: Card renderer

**Files:**
- Create: `src/montaj/dashboard_cards.py`
- Test: `tests/test_dashboard_cards.py` (create)
- Modify: `src/montaj/dashboard.css`

**Interfaces:**
- Consumes: Task 1 names; `BoardContext`, `busy_html`, `link_button`, `format_usd`, `BASE_PATH`, `STATE_LABELS` from `dashboard_parts`; `board.INGEST_STEP`, `board.FIX_GUIDANCE`.
- Produces: `cards_html(view: BoardView, rows: list[BoardRow], ctx: BoardContext, visible: list[int]) -> str`, an `<ol class="cards" aria-label="Active runs">` with one `<li class="card">` per visible source id, then `legend_html()`; `card_html(card: SourceCard, row: BoardRow, ctx: BoardContext) -> str`; `stage_bar_html(card: SourceCard) -> str`. `_resume_html`, `_action_html`, `_note`, `_notes_html` move here unchanged.
- Card markup the tests pin: `<div class="thumb" aria-hidden="true">{duration}</div>`; a title link to `/dashboard/sources/<id>`; `#<id> · <niche>`; the chip `<span class="badge {tone}">{label}</span>`; `<div class="stage-bar" role="img" aria-label="Stage progress: Ingest done, Transcribe done, Candidates next, Rank waiting, …">` with 11 `<span class="seg {word}">`; `<p class="bar-meta">` holding `Stage N of 11 · {label} · {free|paid} stage` and `Source budget {cost} of {ceiling} · {left} left`; the notes list (`_notes_html`); `<div class="next">` with a `<strong>` sentence, a muted sentence and the action button.
- Next-action sentences (bold / muted): `resume`: "Resume from {stage}" / "Runs every unfinished stage through Draft. The estimate is shown before anything is paid."; Retry (action label "Retry"): "Retry {stage}" / same muted line; `review`: "{n} checked clip(s) ready for review" / "Nothing is exported until you approve it."; `export`: "{n} approved clip(s) ready to export" / "Review is done for these clips."; `fix`: "{n} flagged clip(s) need a fix" / `FIX_GUIDANCE`; no action and not permitted: "Blocked" / the permission reason; no action and not imported: "Waiting for import" / `INGEST_STEP`; otherwise "Nothing to run" / "". Plural by count ("1 checked clip", "2 checked clips").
- Legend: `<ul class="legend" aria-label="Stage legend">` with "11 stages" then Done, Running, Next, Blocked, Failed, Waiting, each a `<span class="seg {word}" aria-hidden="true">` plus its word.

- [ ] **Step 1: Write the failing tests** in `tests/test_dashboard_cards.py` using `_add_source`, `_add_clip`, `_imported` and a local helper `_cards(db_path, *, running=None, operations=True)` that builds `rows = build_rows(settings, running=running)`, `view = build_view(rows, source_ceiling=..., run_ceiling=...)`, a `BoardContext` and returns `cards_html(view, rows, ctx, [row.snapshot.source_id for row in rows])`. The page is not switched yet, so no test here reads the page. Pin each of:
  - `test_bar_aria_label_lists_every_stage_and_state`: an imported source with transcript, candidates and rank saved: the label is `Stage progress: Ingest done, Transcribe done, Candidates done, Rank done, Cut next, Captions waiting, Draft waiting, Check waiting, Review waiting, Export waiting, Post log waiting` (adjust to the board's real cell states; derive each word from the seeded checkpoints and say so in the ledger if the board judges differently), and the bar has exactly 11 `class="seg `.
  - `test_chip_and_bar_states_in_html`: one case per chip (Blocked via `allowed=False`; Failed via a source with `speech_status="failed"`; Ready for review via a `CHECKED` clip; Ready to export via `APPROVED`; Needs your confirm via an imported source with a transcript; Waiting via a source with nothing imported; Done via exported clips with every stage saved), asserting the chip text and one bar word per state (`running` via `running=(7, "batch")`, `blocked`, `failed`, `next`, `done`, `waiting`).
  - `test_blocked_source_card_has_no_actions_and_says_why` (Review Focus 1): both `allowed=False` and `basis=" "` sources: the card has no `<button`, no `<form`, no `/confirm`, and the reason text once.
  - `test_busy_board_disables_paid_actions` (Review Focus 2): `running=(2, "batch")` with source 7 resumable: the card shows a disabled button and `Another job is running (source 2)`; `running=(7, "batch")`: the source's chip is `Running` and `Running</span>` appears; a `CHECKED` clip's `Open review` link is still a link.
  - `test_resume_and_retry_open_the_cost_confirmation`: the Resume link is `/dashboard/sources/7/confirm?stage=batch`; a failed source shows `Retry` with the same link; on a read-only page neither appears.
  - `test_budget_line` (Review Focus 3): cost 0.012345 of a 0.5 ceiling reads `Source budget $0.01 of $0.50 · $0.49 left`; cost above the ceiling reads `$0.00 left`; cost 0.0002 reads `&lt;$0.001` and no raw `<$0.001`.
  - `test_title_is_escaped` (Review Focus 4): a title `<b>x</b>` renders as `&lt;b&gt;x&lt;/b&gt;`.
  - `test_next_sentences`: each sentence above for its action, including singular and plural clip counts.
  - `test_legend`: the legend lists "11 stages" and the six words.

- [ ] **Step 2: Run and watch them fail.** Run: `.venv\Scripts\python.exe -m pytest tests/test_dashboard_cards.py -q`. Expected: FAIL (the page still renders a table).

- [ ] **Step 3: Implement** `dashboard_cards.py` and move the four helpers into it; `dashboard.py` imports them from there so the table keeps working until Task 3.

- [ ] **Step 4: CSS** in `dashboard.css`, using existing tokens only: `.cards` (list reset, hairline-separated cards), `.card` (grid: `.thumb` then body; stacks below `40rem`), `.thumb` (fixed aspect block in `--raised`, duration text, `.45rem` radius), `.card-body` head row (title link, `#id · niche`, chip), `.stage-bar` (11 equal segments, `.2rem` gap), `.seg` states exactly as `DESIGN.md`: `done` solid `--accent`; `running` half `--accent` over `--stroke` (a hard-stop gradient); `next` `--stroke` fill with a 2px `--accent` inset outline; `blocked` diagonal `--warning` stripes (`repeating-linear-gradient`); `failed` solid `--warning`; `waiting` `--stroke`. `.bar-meta` (two spans, space-between, wraps), `.next` (bold sentence, muted sentence, button right), `.legend` (wrap, small swatches), `.badge.running` (accent ink on `--raised`). Overflow-wrap anywhere on titles.

- [ ] **Step 5: Gate and commit.** Commit: `Render source cards with an 11-stage bar`.

---

### Task 3: Swap the table for the cards, clean up, check every state

**Files:**
- Modify: `src/montaj/dashboard.py`, `src/montaj/dashboard.css`, `DESIGN.md`, `tests/dashboard_helpers.py`, `tests/test_dashboard_board.py`, `tests/test_dashboard_cards.py`

**Interfaces:**
- Consumes: Task 2.
- Produces: `_overview` builds `view = build_view(...)` once (the same object feeds `summary_html`), renders the "Active runs" panel (`<section class="queue">` with `<h2 id="sources">Active runs</h2>`, the filters nav in `.panel-head`, then `cards_html(...)`, or `_empty_html(...)` when nothing is visible), and keeps the right column (Recent outputs placeholder, Resources).

- [ ] **Step 1: Update the tests first.** In `tests/dashboard_helpers.py` redefine `_rows(page)` to return the HTML between `<ol class="cards"` and its closing `</ol>`. Run the suite and list what fails: expect the old strip assertion in `tests/test_dashboard_board.py` (the `<li class="cell done">` lines in `test_restarted_dashboard_shows_checkpoints_and_resume`), the `Source table` and table-header assertions, and nothing in `tests/test_board.py` (it tests `build_rows`). Rewrite the strip assertion to the new bar: after a restart with transcript, candidates and rank saved, the aria-label contains `Transcribe done` and `Rank done`, and the Resume link is present. Add:
  - `test_three_filters_list_the_right_sources` (Review Focus 5): sources for each view; each filter link's count and the cards listed match; `view=bogus` falls back to all.
  - `test_empty_board_and_empty_filter_messages`: no sources shows "No sources yet"; a filter with no match shows "No sources match this view" and the "Show all sources" link.
  - `test_home_has_no_table`: no `<table`, no `class="strip"`, no `table-wrap`, and the heading `Active runs` is present.
 

- [ ] **Step 2: Run and watch them fail.** Run: `.venv\Scripts\python.exe -m pytest tests/test_dashboard_board.py tests/test_dashboard_cards.py -q`. Expected: the page-level tests FAIL (table still rendered).

- [ ] **Step 3: Implement the swap** in `_overview`, and delete `_table_html`, `_row_html`, `_cell_html` and the imports they alone used (ruff finds them). `dashboard_response` already builds the view; pass it through instead of rebuilding `summary` separately.

- [ ] **Step 4: Run the tests.** Expected: PASS.

- [ ] **Step 5: CSS cleanup.** Remove the table and strip rules (`table`, `thead`, `th`, `td`, `tbody` rules, `.table-wrap`, `.strip`, `.cell*`, `.cell-state`, `.numeric`, `.optional`, `.action-cell`, `.status-cell`) only after `Grep` shows nothing else uses them: the source page uses `.cell-name`, `.cell-state`, `.stage-lines` and `.steps`, so keep those. Remove the `@media (max-width: 39rem)` rules that only styled the table (`.optional`, `th:nth-child(2)`). Keep `.board`, `.heading` and `.toolbar` (source page).

- [ ] **Step 6: `DESIGN.md`.** Add an **Active run card** bullet (thumbnail placeholder, title and meta, chip, bar, budget line, next action, legend) and update the **Chip** bullet with the eight chip names and tones.

- [ ] **Step 7: Visual check of all states at two widths.** Seed a demo database in the scratchpad (never the real `data/`) with one source per chip: blocked, failed, ready for review, ready to export, needs your confirm, waiting, done, plus one over its ceiling and one with a long HTML-looking title. For the Running state run the board through a scratch script outside the repo that patches `JobQueue.running` and `JobQueue.run_spend` (start with `.venv\Scripts\python.exe -c "from montaj.cli import app; app()" dashboard --port 8765`, `python -m montaj` does not work here). Screenshot at 1366px and 375px (`resize_window`, then reset to desktop). Expected: every chip has words; the bar shows each segment style; one primary button per card; no sideways page scroll at 375px (`document.documentElement.scrollWidth <= innerWidth`); the stripe and outline are distinguishable without colour. Fix what fails; stop the server.

- [ ] **Step 8: Gate and commit.** Commit: `Replace the source table with Active runs cards`.

Final step for the executor: the whole-branch review (Opus), then the PR body per the repo's conventions (what changed, the "Calls made" above, the checks run, "MJ-24").
