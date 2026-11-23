# English Production Jobs Implementation Plan

**Goal.** Submit a permitted English video and campaign once and produce a resumable batch for human review.

**Architecture.** A shared job runner composes ingest and the existing pipeline stages. The local dashboard submits jobs and shows their progress. Review and export use the existing campaign and approval rules.

**Tech stack.** Python 3.12, Typer, SQLite, FFmpeg and the existing loopback dashboard.

## Job runner

- [x] Register `montaj job LOCATION --campaign ID --niche NICHE --allowed --basis TEXT --top N` and `montaj job --resume SOURCE_ID --top N`.
- [x] Use the stored campaign's clip bounds, English transcription and a shared budget. Discover and rank once, check duration, blocked terms, overlap and source bounds before rendering, then process eligible candidates in rank order.
- [x] Backfill failures and report a batch shortfall when the budget or candidate pool is exhausted. Preserve human decisions and reuse saved stages when resuming.
- [x] Test permission, campaign bounds, backfill, budget stops and reruns using fixture media and fake providers.

## Dashboard and batch review

- [x] Add a loopback-only job form for individual YouTube video URLs and files in the Montaj inbox. Run one dashboard job at a time and show stage, run spend and retry controls.
- [x] Accept writes only on the operations dashboard with a same-origin form token. Keep the standalone preview read-only.
- [x] Add source timestamps, check findings and human approval controls to the batch preview. Use the existing export checks to package only approved clips from the selected source.
- [x] Test validation and review/export guards, run Ruff, mypy and pytest, and open the implementation PR.
