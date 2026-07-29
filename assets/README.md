# Marshal assets

## Colour palette

| Token | Hex | Use |
|-------|-----|-----|
| Marshal Orange | `#FF6B1A` | All mark geometry, wordmark logotype, flow arrows |
| Label Grey | `#6B7280` | Architecture box strokes, labels, merge-back arrow |
| Surface Grey | `#F3F4F6` | Architecture box fills |

## Files

### `logo.svg`
The Marshal mark on a transparent background: one origin fanning out to four lanes of unequal
length, each ending in an agent. Use for light backgrounds, favicons, and general brand placement.
Square viewBox, no width/height, so it scales freely. Verified legible down to 32x32.

### `logo-dark.svg`
Identical geometry to `logo.svg`. The same `#FF6B1A` orange achieves ≥ 4.7:1 contrast against
typical dark surfaces (`#1A1A1A` and darker), so no colour adjustment is applied — a lighter tint
would weaken brand consistency without a real legibility gain.

### `logo-mono.svg`
Same geometry with `stroke="currentColor"` and `fill="currentColor"`. Drop into any HTML or SVG
context and set `color` (or a parent's `fill`/`stroke`) to control the colour. Intended for docs,
dark-mode stylesheets, or single-colour print.

### `wordmark.svg`
The mark plus the "Marshal" logotype in `system-ui, -apple-system, "Helvetica Neue", Arial,
sans-serif` at `font-weight: 600`. System font chosen over embedded paths to keep the file small,
and because the stack resolves to SF Pro / Segoe UI / Roboto everywhere Marshal runs. The weight
matches the 4-unit stroke of the mark.

### `architecture.svg`
Flat diagram of the Marshal runtime: Driver Agent → MCP Server → Fleet → N isolated worktrees
(each running a backend adapter) → integrate back. Uses the same orange for active data-flow
arrows and neutral grey for boxes, labels, and the merge-back return path (shown dashed).
Label text uses the same system font stack as the wordmark.
