# Design system: Montaj board

The visual rules for the local production board (`src/montaj/dashboard.css`) and for the design comps made from it. Tokens are copied from that stylesheet. If the two ever differ, the stylesheet wins and this file is wrong.

Mode: Operate. One operator, working through a repeating batch, scanning state and clicking one next action. The interface should disappear into the task.

## Principles

1. Real state, never invented state. A failed or blocked item says what happened and what to do next.
2. Cost is visible before and after it is spent. A paid stage shows its estimate and the source total it would reach.
3. One obvious next action per source. Paid actions ask for a confirm click. A gated action is disabled and says why.
4. Compliance gates are visible: `source_allowed`, the check, human review.
5. State is never colour alone. Every colour pairs with text, an icon, or a pattern.

## Colour

Dark only. Neutrals are tinted toward hue 276 (violet). The accent is lime and is used for the primary action, the current selection, and progress. Semantic colours are separate from the accent.

| Token | Value | Use |
|---|---|---|
| `--canvas` | `oklch(18% .016 276)` | page |
| `--rail` | `oklch(15.5% .016 276)` | second neutral layer: inputs, legend bars, sidebar |
| `--surface` | `oklch(22% .02 276)` | panels |
| `--raised` | `oklch(25.5% .025 276)` | hover, selected, skeleton blocks |
| `--stroke` | `oklch(33% .025 276)` | borders, dividers, empty progress tracks |
| `--ink` | `oklch(96% .012 276)` | text |
| `--muted` | `oklch(73% .025 276)` | secondary text |
| `--accent` / `--accent-ink` | `oklch(89% .17 126)` / `oklch(23% .06 126)` | primary button, progress, focus ring |
| `--warning` / `--warning-bg` | `oklch(87% .119 76)` / `oklch(31% .053 71)` | blocked, failed check, cost rising |
| `--review` / `--review-bg` | `oklch(86% .079 292)` / `oklch(32% .065 288)` | waiting for a person, ready for review |
| `--success` / `--success-bg` | `oklch(87% .093 163)` / `oklch(31% .055 168)` | live, cost falling, savings |

Rules: tint secondary text from the neutrals, never plain grey. Cost going up is warning, cost going down is success, neutral volume is muted. Never use the accent for decoration.

## Type

Inter, with `"Segoe UI"` and `system-ui` as fallbacks. One family carries headings, labels and data, as the shipped board does. Numbers use `font-variant-numeric: tabular-nums` everywhere.

| Role | Size | Weight | Tracking |
|---|---|---|---|
| Page title (h1) | 1.6rem (2.15rem on the shipped heading) | 670 | -.045em |
| Summary figure | 2rem | 650 | -.04em |
| Panel title (h2) | 1.05rem | 660 | -.015em |
| Row title | .95rem | 620 | -.01em |
| Body and labels | .8 to .9rem | 500 to 620 | 0 |

Scale steps stay between 1.125 and 1.2. Prose is capped at 65ch.

## Shape and space

Panel radius `.85rem`. Controls `.44rem` to `.5rem`. Tags and chips `.3rem`. Thumbnails `.45rem`. Panels are `--surface` with a 1px `--stroke` border and no shadow. Inside a panel, rows are separated by hairlines, not nested cards. Summary figures share one panel and are divided by 1px gaps.

## Components

- **Button.** `.button` (surface, stroke border) and `.primary` (accent). Min height 2.2rem, 2.75rem for New run. Hover lifts to `--raised` and a `--muted` border. Disabled drops to 45% opacity and does not react to hover.
- **Chip.** Tinted background plus matching ink, `.3rem` radius, text always present. Running uses accent ink on `--raised`, needs-confirm uses review, blocked and failed use warning.
- **Stage bar.** 11 equal segments, one per stage. Done is solid accent, running is half accent, next is an accent outline, blocked is a diagonal warning stripe, waiting is `--stroke`. The container carries an `aria-label` listing every stage state, and a legend sits under the list.
- **Next action.** A bold sentence naming the stage and its estimate, a muted sentence giving the source total after it, and one button. Paid stages use the primary button only when ready.
- **Switch.** `role="switch"` button, accent track when on, `--rail` track when off, value text beside it. Off shows a warning line that spend is not capped.
- **Row.** Thumbnail, title and meta, chip, one action. Hover is `--raised`.
- **Focus.** 2px accent outline, 3px offset, on every control.
- **Browser surfaces.** Selection is accent on accent-ink. Scrollbars use stroke on rail.

## States

Every list has an empty state that teaches the next step, a loading skeleton in `--raised` blocks (no spinner in the middle of content), a failed-job state with the retry cost, and a locked-source state with the age of the lock. Their comps live on the "Board states" artboard of the design canvas.

## Motion

150 to 250 ms, ease-out, only on state change (hover, switch, button). No page-load choreography.

## Out of scope

Analytics dashboards and API-posting UI wait for the week 3 checkpoint. Approve and reject stay in the clip preview. Light theme is not planned.
