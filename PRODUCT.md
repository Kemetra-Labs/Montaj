# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

One operator running Montaj alone, on a local machine, on a remote server through an SSH tunnel, or both. They process permitted long videos into reviewed vertical clips and post them manually. Their work is a repeating batch: add a source, run stages, wait on paid jobs, review clips, export, log posts. Multi-user support is out of scope.

## Product Purpose

Montaj turns long videos from permitted sources into reviewed vertical clips with captions, hooks and a post log. The operator watches every clip before posting. Success for the control board: one screen shows the state of every source and clip and runs pipeline stages, so the operator rarely needs the terminal to see or advance work.

## Positioning

A single-operator production tool with compliance built in. Each source must be marked allowed, a human reviews every clip, and the compliance check gates approval and export. Posting is manual or through official APIs, never proxies or browser tricks.

## Operating Context

Stages are separate commands that read and write files: ingest, transcribe, candidates, rank, cut, captions, draft, check, review, export, post-log. Paid stages (Deepgram transcription, Claude discovery, ranking and drafting) are bounded by run and source budgets, and a stage output is reused when its input hash still matches. A source takes a file lock, so one command writes a source at a time. The dashboard runs one background job at a time. The board is served on 127.0.0.1 only; remote use goes through an SSH tunnel.

## Capabilities and Constraints

- Existing surfaces: the Pipeline Control board at `/dashboard` (read only under `preview serve`), which starts, resumes and retries one job at a time behind a same-origin form token; a source page with Run, Retry and Rerun per stage; and the clip preview at `/` with review controls.
- The board must keep to loopback, require a same-origin token for every write, and never skip the `source_allowed` permission, the compliance check or human review.
- Paid stages show an estimated cost and need a confirm click. Free stages run on one click.
- Approve and reject stay in the clip preview. The board shows review counts and links to it.
- Starting a new source (YouTube URL or inbox file, with permission and basis) moves onto the board.
- No analytics dashboards or API-posting UI until the week 3 checkpoint justifies them.
- Terminology: source, clip, stage, job, campaign, niche, checkpoint, check, review, export, post log.

## Evidence on Hand

Real data lives in `data/montaj.db`: sources, clips with status, api_calls with costs, campaigns, accounts, posts. Existing dashboard code is in `src/montaj/dashboard.py`, `dashboard_jobs.py`, `preview.py`, `job.py`, and its styles in `src/montaj/dashboard.css`. No screenshots, user research or analytics exist.

## Product Principles

1. The operator stays in control. Nothing posts or approves without a human action, and no stage runs without a click.
2. Show real state, never invented state. Failures are named with a next step, never silently skipped.
3. Cost is always visible before and after it is spent.
4. One obvious next action per source.
5. Compliance gates are visible, not hidden. A blocked stage says why.
