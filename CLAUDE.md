# CLAUDE.md

Montaj is a planned Python command-line tool for one operator. It turns long videos from permitted sources into reviewed vertical clips with captions, hooks and a post log. Read [the architecture](docs/ARCHITECTURE.md) before big changes.

## Rules that never change

- Only process sources marked `source_allowed`. Refuse otherwise.
- A human reviews every clip before posting. No auto-posting without review.
- No proxies, fake browsers or fingerprint tricks. Official APIs or manual posting only.
- Never commit secrets, API keys, cookies, source videos or rendered clips. Use environment variables and keep `.env` and runtime data out of git.

## Pipeline (each stage is its own command, reads and writes files)

`ingest` → `transcribe` → `candidates` → `rank` → `cut` (raw render) → `captions` → `draft` → `check` → `review` → `export` → `post-log`

The compliance check runs after captions and drafting and again after any change in review. Approval and export require a current check without failures. [Issue #48](https://github.com/Kemetra-Labs/montaj/issues/48) defines clip status, file paths and rerun rules and will add `docs/pipeline-contract.md`.

Reuse a successful stage output when its input hash still matches. Check the configured run and source budgets before each paid API call.

## Stack

Python 3.12, `uv`, `typer` (CLI), `pydantic` (models), SQLite (`data/montaj.db`), `pytest`, `ruff`, `mypy`. External: `yt-dlp`, `ffmpeg`, Deepgram Nova-3 API, Claude API, optional MediaPipe or OpenCV for face tracking (talking-head sources only).

## Planned layout

```
src/montaj/        package (cli.py, stages/, models.py, db.py, config.py)
prompts/           Claude prompts as files, versioned
tests/             pytest, with small fixture clips and canned API responses
data/              git-ignored: sources, transcripts, clips, db
docs/              ARCHITECTURE.md
```

## Commands after project setup

```
uv sync                      # install
uv run montaj --help         # CLI
uv run pytest                # tests
uv run ruff check . && uv run ruff format --check .
uv run mypy src
```

Once project setup provides the tooling, run ruff, mypy and pytest before every commit. All three must pass.

## Conventions

- Type hints everywhere. Pydantic models for anything crossing a stage boundary.
- Stages are small functions with plain inputs and outputs. No hidden global state.
- Wrap each external service (Deepgram, Claude, yt-dlp, ffmpeg) in a small class so tests can swap it for a fake. Tests never call real APIs.
- Log every Claude and Deepgram call with input size and estimated cost.
- Prompts are files in `prompts/`, not inline strings. Change a prompt in its own commit.
- Word-level timestamps are the source of truth for cuts and captions.
- Keep functions short, errors explicit. Fail with a clear message, never silently skip a clip.
- Commit messages: short, imperative. One issue per PR, link it (`Closes #N`).
- Small PRs. If an issue needs a design choice not in the architecture doc, write it in the PR description.

## Current phase

Follow the [v3 build order](https://github.com/Kemetra-Labs/montaj/issues/31#issuecomment-7079949498). Phase 1 ends with the first real run and uses manual posting. Start Phase 2 metrics and reporting after Phase 1 is merged and the [first real run](https://github.com/Kemetra-Labs/montaj/issues/30) passes. API posting also needs the week 3 checkpoint and owner approval. The only web page is the read-only local preview.

Aim for ten approved clips in the first real run. Approve fewer if needed and record what limited the count.
