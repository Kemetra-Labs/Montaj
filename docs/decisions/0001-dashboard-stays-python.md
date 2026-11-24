# 0001: The board stays server-rendered Python

Status: accepted, 2026-11-24.

## Context

The production board (`/dashboard`, source detail, jobs, clip preview) is rendered by Python in `src/montaj/dashboard*.py` and styled by `src/montaj/dashboard.css`. We considered moving it to a React frontend, with Python keeping only the pipeline work.

Montaj has one operator on one machine, or on a server behind an SSH tunnel. The board binds to loopback and every write needs a same-origin token. The stack in `CLAUDE.md` is Python, `uv`, `typer`, `pydantic` and SQLite.

## Decision

Keep the board server-rendered from Python. Add small JavaScript only where a page needs it, such as live job progress. Do not add a Node toolchain or a React frontend now.

## Why

- React adds a build step, a JSON API for every screen, JavaScript tests and a second CI job. For one operator that cost is larger than the benefit.
- Every new endpoint widens the write surface that the same-origin token has to protect.
- Phase 1 ends with the first real run and manual posting. The board does not gate that.
- Live updates do not need a framework. Polling or server-sent events on the existing pages is enough.

## Consequences

- No Node, no `dist/` and no component library. Claude Design sync (`/design-sync`) does not apply to this repo. The design rules live in `DESIGN.md`.
- Keep dashboard data (source state, stage state, cost, budget headroom) in functions separate from the code that writes HTML. A later move to another frontend then only replaces the rendering.

## Revisit when

- The board grows many screens that depend on each other, such as filtering, multi-select or clip review inside the board.
- A second operator is added.
- The week 3 checkpoint shows the board is the bottleneck.
