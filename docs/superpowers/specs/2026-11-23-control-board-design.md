# Control board design

Status: draft for approval. Product context: [PRODUCT.md](../../../PRODUCT.md).

## Intent

The operator wants one screen that shows every source and clip and runs pipeline stages from it. Today they switch between the read-only production board (`/dashboard`), a separate jobs page (`/dashboard/jobs`), the clip preview (`/`) and the terminal. The control board replaces that switching for everything except watching and approving clips.

Success: the operator rarely needs the terminal to see or advance work, and always knows what the next action is, what it will cost, and why anything is blocked.

## Goals

- One home screen listing every source with its stage progress, clip counts, spend and one next action.
- A detail pane for one source with Run/Retry per stage, the live job state and its budget.
- Start a new source (YouTube URL or inbox file, with permission and basis) from the board. This retires the separate jobs page.
- Show estimated spend across all sources, the running job's spend against its run ceiling, sources needing attention, and clips awaiting review. Montaj has per-run and per-source budget ceilings but no daily cap, so there is no "spend today against the cap".

## Non-goals

- Approve and reject. These stay in the clip preview. The board shows review counts and an "Open review" link.
- Analytics, metrics or earnings views, and API posting. Both wait for the week 3 checkpoint in `docs/ARCHITECTURE.md`.
- Multi-user support, authentication beyond the existing same-origin token, or a job queue that survives a restart.
- A visual redesign. The board extends the existing dark theme, accent, badges and rail in `src/montaj/dashboard.css`.

## Anti-goals

- Any action that skips `source_allowed`, the compliance check or human review.
- Any write without the same-origin token, or any non-loopback bind.
- A paid stage that starts without showing its estimated cost first and getting one confirm click.
- Silent failures. A failed or blocked stage always says what happened and what to do next.

## Constraints

- **Loopback only.** Served on 127.0.0.1. Remote use is through the SSH tunnel in `docs/server.md`, so pages stay light.
- **One job at a time.** `JobQueue` in `src/montaj/dashboard_jobs.py` holds one active job in memory. A second submission is rejected. Restarting the dashboard clears live status, and the operator resumes by source id. MJ-8 (`docs/engineering/mj-6-to-mj-9-feasibility.md`) deferred a durable queue (Huey).
- **Today a dashboard job is the whole batch.** `JobQueue._work` calls `start_job` then `produce_batch`. There is no per-stage entry point in the dashboard. Per-stage Run/Retry needs a new thin layer over the existing stage functions (`ingest`, `transcribe`, `candidates`, `rank`, `cut`, `captions`, `draft`, `check`) that reuses the source lock, budgets and input-hash reuse. `pipeline.next_stage` already computes a clip's next stage.
- **Source lock.** Each stage that writes a source takes `data/sources/<id>/.lock`. A stage the lock refuses reports who holds it.
- **Stack.** Python 3.12, standard-library HTTP and server-rendered HTML as in `dashboard.py`. No frontend framework is introduced unless the builder shows the existing approach cannot give a live job log.
- **Accessibility.** Keyboard operable, visible focus, status never carried by colour alone, contrast at least WCAG AA. Works at laptop width and on a narrow window.

## Approaches considered

Three layouts were mocked up and compared with the operator.

- **A. Pipeline lanes (Kanban).** Columns are stages, cards are sources and clips. Shows the bottleneck at a glance. Rejected: with a handful of sources it feels empty, and it gives little per-source detail.
- **B. Source table with stage steppers.** One row per source, a stage progress strip, one next-action button. Closest to the current board, so the lowest cost.
- **C. Control room (three panes).** Source list, stage list for the selected source, live log and budget. Most like a controller, but it shows one source at a time and is the heaviest build.

Chosen: **B as the home screen, with C's detail pane opening on a row click.** B gives the overview, C's pane gives per-stage control. Chosen on the operator's say-so after a recommendation, with the reasoning above.

## Design

### Home: the source table

- Top bar: estimated spend across sources (and the running job's spend against its run ceiling), count of sources needing attention, count of clips awaiting review, and a "+ New job" action.
- One row per source: title and id, the stage strip, clip counts, spend, and one primary button naming the next action ("Ingest", "Retry cut", "Open review", "Export").
- Stage strip, in order: ingest, transcribe, candidates and rank, cut and captions, draft and check, review, export. Each cell is done, running, blocked, failed or not started, shown by shape or label and not only colour.
- Filters match today's views: all, needs attention, awaiting review.

### Detail pane for one source

- Header: title, state badge, permission basis, campaign, total spend against its source cap.
- One line per stage with its status, a Run or Retry button where allowed, and a "why blocked" line otherwise.
- A live job panel: current stage, run spend, elapsed time, and the latest result or error in fixed, safe wording as today's `_problem` does. The panel refreshes while a job runs.
- Link to the clip preview for this source.

### New job

A form on the board with URL or inbox filename, campaign, niche, the permission checkbox with basis, target count and an optional budget. The same validation as `Submission` and `dashboard_location` applies. The jobs page redirects to the board.

### Running a stage

- Free stages (cut, captions, check) run on one click. Paid stages (transcribe, candidates and rank, draft) show an estimated cost from the existing run preview and need one confirm click.
- While a job runs, every other Run button is disabled with "Another job is running (source N)". There is no queue.
- A stage whose inputs are unchanged reports "Up to date" and offers an explicit rerun rather than spending silently.

### States

Empty (no sources, with a first-run pointer to "+ New job"), running, failed with a named next step, over run or source budget, source locked by another process, dashboard restarted mid-job (live status is gone, so the row shows the saved checkpoints and offers Resume), and compliance check failing (approve and export blocked, reason shown).

## Calls made while writing this

These were settled without asking the operator, because a sensible builder could decide them without changing what gets built. The operator can overrule any of them.

- **Primary row button.** "Resume" runs the existing `produce_batch` path for that source, which skips stages whose inputs are unchanged. It is the one tested path today. The per-stage buttons in the detail pane run single stages.
- **Which clips a per-stage Run touches.** The batch's selected clips (top N, default `DEFAULT_TARGET`). Retry touches only failed clips, and human decisions on approved clips are preserved as `produce_batch` does today.
- **Grouped strip cells.** A cell that groups stages (cut and captions, draft and check) runs its stages in order and stops at the first failure.
- **No queue.** When a job is running, other Run buttons are disabled with a reason. They do not queue.

## Left to the builder

- Exact visual details inside the existing theme, layout breakpoints and wording.
- How the live job panel refreshes (meta refresh or small polling script) and whether the table auto-refreshes or stays manual.
- Whether per-stage Run is one generic endpoint or one per stage.
- Test layout, following existing `tests/test_dashboard.py` patterns with fake providers.

## Open decisions for the operator

None at design level. Anything listed under "Left to the builder" is the builder's call.
