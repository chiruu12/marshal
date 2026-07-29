# Marshal assets

## Colour palette

| Token | Hex | Use |
|-------|-----|-----|
| Marshal Orange | `#FF6B1A` | The mark, the logotype, and active data-flow arrows |
| Label Grey | `#6B7280` | Architecture box strokes, labels, merge-back arrow |
| Surface Grey | `#F3F4F6` | Architecture box fills |

## Files

### `logo.svg`
The Marshal mark: an angular three-point crown. Use for light backgrounds, favicons, and general
brand placement. Square viewBox with no width/height, so it scales freely. Verified legible at
32x32.

### `logo-dark.svg`
Identical geometry to `logo.svg`. `#FF6B1A` clears WCAG AA against `#1A1A1A` and darker, so the
colour is unchanged; a lighter tint would cost brand consistency for no legibility gain.

### `logo-mono.svg`
Same geometry with `fill="currentColor"`, so the mark inherits from its context (a CSS `color`, or
`fill` on a parent). For docs, dark-mode stylesheets, and single-colour print.

### `wordmark.svg`
The mark plus the "Marshal" logotype in `system-ui, -apple-system, "Helvetica Neue", Arial,
sans-serif` at weight 600. A system stack rather than converted paths: it resolves to SF Pro /
Segoe UI / Roboto everywhere Marshal runs, and keeps the file under 1 KB.

### `architecture.svg`
Flat diagram of the runtime: driver agent to MCP server to Fleet, fanning out to N isolated
worktrees each running a backend adapter, then merging back through review. Orange marks active
data flow; grey marks structure and the return path.
