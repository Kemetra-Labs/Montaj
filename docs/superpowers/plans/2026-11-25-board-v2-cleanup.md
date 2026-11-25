# Board v2 cleanup and docs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Nothing of the old overview is left in code, CSS or docs. `DESIGN.md`, `docs/server.md` and the control board spec describe the board as built. An accessibility pass at laptop and narrow width finds no failure: keyboard order, visible focus, contrast, sideways scroll, and no state carried by colour alone.

**Architecture:** No new behaviour. Remove the few leftovers the earlier tickets did not reach, rewrite the stale doc paragraphs, and add one durable guard: a test that checks the colour tokens in `dashboard.css` against WCAG AA contrast.

**Tech Stack:** Python 3.12, server-rendered HTML strings, `dashboard.css`, pytest, ruff, mypy.

**Spec:** Jira [MJ-29](https://rahmaqanater.atlassian.net/browse/MJ-29), epic [MJ-21](https://rahmaqanater.atlassian.net/browse/MJ-21) (its "Must keep working" list is the guardrail).

## Global Constraints

- Delete only what the new board replaces. Everything in MJ-21's "Must keep working" list keeps working, unchanged:
  - filters;
  - the New job form with its ceiling confirmation;
  - the live job panel that refreshes while a job runs;
  - Resume and Retry cost confirmation;
  - per-stage Run on the source page;
  - Open review and Export links;
  - the notice banner and the 503 "Dashboard unavailable" page;
  - the same-origin token on every write;
  - loopback only;
  - read only under `preview serve`;
  - one job at a time, with other buttons disabled and a reason shown.
- `tests/test_dashboard_forms.py` (MJ-28's pinned inventory of forms, buttons and action links) passes unchanged.
- Done when `grep` finds no reference to a removed name, `ruff`, `mypy` and `pytest` pass, and the PR lists what was deleted.
- Before each commit: `.venv\Scripts\python.exe -m ruff check . ; ... ruff format --check . ; ... mypy src ; ... pytest` (`uv run` hangs here). Known on a clean `main`: 3 mypy import errors (cv2, numpy) and two environmental test failures (`test_engine::test_recipe_tracks_vision_availability_niche_and_loudness`, `test_speech::test_limits_download_errors_and_permission_basis`).
- Commits end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. One PR; the body says "MJ-29".

## What a search of the code found (planning, 2026-11-25)

Each Board v2 ticket deleted what it replaced: the strip, table, topbar and the Workload and Resources side panel are already gone, and tests guard their absence. A search for top-level names with no reference outside their own definition, CSS classes no Python file emits, and dataclass fields nothing reads found only these leftovers:

| Leftover | Where | Why it is dead |
| --- | --- | --- |
| `NEW_JOB_LINK = "#new-job"` | `dashboard_parts.py:10` | The old empty state was its last user; MJ-27 replaced it. |
| `.sr-only` | `dashboard.css:134` | No markup uses the class. |
| `SourceCard.state`, `SourceCard.tone` | `board_view.py:78-79` | Copies of the old overview's status column; no renderer reads them. The chip replaced them. |
| `SourceSnapshot.state` and the `state` half of `_snapshot`'s state/tone pairs | `dashboard.py:53`, `dashboard.py:~106-120` | Only fed `SourceCard.state`. `SourceSnapshot.tone` stays: "Needs attention" counts it. |

Stale docs: in `DESIGN.md`,
- the chip bullet says "eight chips" (MJ-27 added Locked and Over budget);
- the button bullet says "New run";
- the stage bar bullet does not say how the 7 judged groups map onto 11 segments;
- the **Next action** bullet describes an estimate the card no longer shows;
- the **Switch** bullet describes a ceiling switch MJ-21 rules out;
- the **Row** bullet describes the old overview row;
- the source page bullet does not say the source page never shows Locked (an MJ-28 deferred minor).

In `docs/server.md`, the board paragraph describes rows and saved checkpoints in the old layout. The control board spec still says "Status: draft for approval".

## Calls made while writing this (the owner can overrule)

- **Remove the status label, keep the tone.** "Needs attention" is computed from `SourceSnapshot.tone`, so the tone and the rules that set it stay. Only the unused word goes.
- **DESIGN.md loses Switch and Row.** No code builds either. A ceiling switch is ruled out by MJ-21. The Row bullet's job is done by the card and the recent output row.
- **The contrast check becomes a test** (`tests/test_design_contrast.py`) that converts the `oklch` tokens to sRGB and computes WCAG ratios. The browser pass then checks the rest: keyboard order, focus, sideways scroll and colour-only state. A test keeps the tokens honest after this ticket; a one-off screenshot would not.
- **The spec's status line** becomes "Built. Its look was replaced by Board v2 (Jira MJ-21) on 2026-11-25; its behaviour stands." The spec's body stays as the record of the decision.
- **The other MJ-28 deferred minors stay deferred:** the next-stage swatch, the shared stage meta helper, and the `.page-title` size on phones. They are polish, not old-overview leftovers.

## Review Focus

1. Removing `SourceSnapshot.state` changes no tone and so no "Needs attention" count. Task 1 pins it with the existing tone tests and the summary strip tests, run unchanged.
2. Every MJ-21 "Must keep working" item still works. Task 1 runs the forms, board, detail, jobs, header and dashboard tests unchanged, apart from the fields removed.
3. A text colour that fails AA on its background is caught by a test, not only by eye. Task 3 pins it.
4. The keyboard reaches every control on the home, the source page and a confirm page in reading order, and each shows a visible focus ring. Task 3 checks it in the browser.
5. The docs name only features that exist (no switch, no "New run", no estimate on the card). Task 2 checks it with a `grep`.

---

## File structure

- Modify `src/montaj/dashboard_parts.py`, `src/montaj/board_view.py`, `src/montaj/dashboard.py`, `src/montaj/dashboard.css`.
- Modify `DESIGN.md`, `docs/server.md`, `docs/superpowers/specs/2026-11-23-control-board-design.md`.
- Tests: `tests/test_board_view.py` and any test that builds `SourceSnapshot(state=…)` (field removed); create `tests/test_design_contrast.py`.

---

### Task 1: Remove the leftovers

**Files:** Modify `src/montaj/dashboard_parts.py`, `src/montaj/board_view.py`, `src/montaj/dashboard.py`, `src/montaj/dashboard.css`; Test `tests/test_board_view.py` and every test that constructs `SourceSnapshot` with `state`.

**Interfaces:**
- Produces: `SourceSnapshot` without `state`; `SourceCard` without `state` and `tone` (positional order of the remaining fields unchanged otherwise). `_snapshot` computes `tone` alone, with the same rules in the same order.

- [ ] **Step 1: Write the guard test first.** `tests/test_board_view.py::test_snapshot_tones_are_unchanged` builds snapshots through `dashboard._load` for one source in each of the seven tone branches:
  - a flagged clip (warning);
  - a checked clip (review);
  - an approved clip (success);
  - an exported clip (success);
  - imported (success);
  - media missing (warning);
  - awaiting ingest (neutral).

  It asserts each `tone`. Run it on the current code: Expected PASS (it pins today's tones).
- [ ] **Step 2: Write the failing removal tests.** `test_removed_names_are_gone`: `not hasattr(dashboard_parts, "NEW_JOB_LINK")`; `"state" not in SourceSnapshot.__dataclass_fields__`; `"state" not in SourceCard.__dataclass_fields__ and "tone" not in SourceCard.__dataclass_fields__`; and `".sr-only" not in` the CSS text. Run: Expected FAIL.
- [ ] **Step 3: Implement.** Delete the four leftovers. Update every test that passes `state=` to `SourceSnapshot` (the `_snapshot` helpers in `tests/test_board_view.py` and any other `Grep` finds). Change no other assertion.
- [ ] **Step 4: Run.** `.venv\Scripts\python.exe -m pytest tests/test_board_view.py tests/test_dashboard_forms.py tests/test_dashboard_board.py tests/test_dashboard_detail.py tests/test_dashboard_jobs.py tests/test_dashboard_header.py tests/test_dashboard.py -q`. Expected: PASS, with the forms and the tone guard unchanged (Review Focus 1, 2). Then `git grep -n "NEW_JOB_LINK\|sr-only\|snapshot.state\|card.state\|card.tone"`: Expected no output.
- [ ] **Step 5: Gate and commit:** `Remove the old overview's last leftovers`.

---

### Task 2: Docs match the board

**Files:** Modify `DESIGN.md`, `docs/server.md`, `docs/superpowers/specs/2026-11-23-control-board-design.md`.

- [ ] **Step 1: `DESIGN.md`.**
  - **Button:** "2.75rem for New job".
  - **Chip:** ten chips, first match wins:
    - Blocked (warning);
    - Running (accent on `--raised`);
    - Locked and Over budget (warning);
    - Failed (warning);
    - Ready for review, Ready to export and Needs your confirm (review);
    - Done (success);
    - Waiting (neutral).
  - **Stage bar:** add that `board.py` judges seven groups, so until per-stage state lands (MJ-30) both stages of a group show the group's state (Candidates and Rank; Cut and Captions; Draft and Check) and Post log shows waiting. Each stage line on the source page is one group.
  - **Next action:** the bold sentence names the step, the muted sentence says what happens next or why ("The estimate is shown before anything is paid."), and one button. No dollar figure on the card.
  - **Source page:** add that it never shows Locked, because it does not read lock files.
  - Delete the **Switch** and **Row** bullets.
  - Name the **thumbnail** placeholder once in Shape and space: a `--raised` block with the duration, no frame.
- [ ] **Step 2: `docs/server.md`.** Rewrite the board paragraph (line 93) for the built board, keeping every fact that still holds. The parts:
  - the header and summary strip;
  - Active runs cards with the 11-stage bar and one next action;
  - Recent outputs;
  - the controls bar with New job, the run ceiling meter and disk free;
  - the special states;
  - the source page with per-stage Run, Retry and Rerun.

  Facts it must keep:
  - read only under `preview serve`;
  - estimates and one confirmation for paid stages, Resume and Retry;
  - New job accepting the ceilings;
  - one job at a time;
  - live status in memory;
  - the batch of 10;
  - `montaj ingest` for import failures;
  - no retry for language or speech failures;
  - review decisions in the clip preview.
- [ ] **Step 3: Spec status line.** As in "Calls made".
- [ ] **Step 4: Check the docs** (Review Focus 5). Run `git grep -n -i "new run\|eight chips\|role=\"switch\"\|switch\.\|Source budget" DESIGN.md docs/server.md`: Expected no output, except where a sentence states the switch is ruled out. Read each changed paragraph against the code once.
- [ ] **Step 5: Commit:** `Describe the built board in the docs`.

---

### Task 3: Accessibility pass

**Files:** Create `tests/test_design_contrast.py`; Modify `src/montaj/dashboard.css` (fixes only).

**Interfaces:**
- Produces: `_oklch_to_srgb(l: float, c: float, h: float) -> tuple[float, float, float]` and `_contrast(fg, bg) -> float` as test-local helpers (OKLab to linear sRGB with the standard matrices, gamma-encode, clamp; WCAG relative luminance).

- [ ] **Step 1: Write the contrast test.** `test_text_tokens_meet_aa` parses the `:root` tokens from `dashboard.css` and asserts a ratio of at least 4.5 for each text and background pair the CSS uses:
  - `--ink`, `--muted` and `--accent` on `--canvas`, `--surface` and `--raised`;
  - `--warning` on `--warning-bg` and on `--surface`;
  - `--review` on `--review-bg` and on `--surface`;
  - `--success` on `--success-bg` and on `--surface`;
  - `--accent-ink` on `--accent`.

  A 3.0 floor applies to `--stroke` against `--surface` only if the CSS uses it as the sole boundary of a control. Check `Grep` for that before adding the pair. `test_conversion_is_right`: `oklch(100% 0 0)` gives white, `oklch(0% 0 0)` gives black, and white on black gives 21.
- [ ] **Step 2: Run it.** Run: `.venv\Scripts\python.exe -m pytest tests/test_design_contrast.py -q`. Expected: PASS when the tokens are fine. A failing pair is a finding: change that token's lightness in `dashboard.css` and in `DESIGN.md`'s colour table together, rerun, and ledger the ruling.
- [ ] **Step 3: Browser pass** on a scratch database (never the real `data/`), with one source in each card state and a recent output, at 1366px and 375px. Check:
  - **Keyboard order:** tab from the top of the home through the skip link, header, summary links, filters, cards, recent outputs and the controls bar, then the source page and a confirm page. The order follows reading order and nothing is skipped or trapped.
  - **Visible focus:** on each control, `getComputedStyle(document.activeElement).outlineStyle` is not `none`.
  - **Sideways scroll:** `scrollWidth` equals the viewport width.
  - **No colour-only state:** every chip, segment state, meter state and disk warning has a word.

  Fix what fails in `dashboard.css` and stop the servers.
- [ ] **Step 4: Gate and commit:** `Check the board's contrast and keyboard path`.

Final step for the executor: the whole-branch review (Opus), then the PR body per the repo's conventions. It covers what changed, the table of what was deleted, the "Calls made" above, the checks run, and "MJ-29".
