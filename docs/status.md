# Implementation status & roadmap

A snapshot of what's built, what's verified, and what's next. For the architecture see
[`design.md`](design.md); for how to use it see [`usage.md`](usage.md).

## What's built

The full vertical slice is in place — driver → MCP → service → fleet → backends:

| Module | Responsibility | State |
|--------|----------------|-------|
| `types.py` | Shared dataclasses + enums | done |
| `backends/base.py` | Abstract backend + safe `run()` (no-stdin, hard timeout) | done |
| `backends/{cursor,opencode,codex,antigravity}.py` | Four adapters off one base class | done |
| `worktree.py` | Git worktree lifecycle (isolation boundary) | done |
| `usage.py` | Per-provider usage (events.jsonl + summary) | done |
| `state.py` | Persistent fleet state (fleet.json) | done |
| `fleet.py` | Orchestrator: worktree → run → record → persist | done |
| `registry.py` | Construct backends by name | done |
| `config.py` | `fleet.config.yaml` → clients, Fireworks guard | done |
| `service.py` | Testable core the MCP/CLI call into | done |
| `cli.py` | `marshal backends/usage/status/mcp` | done |
| `mcp_server.py` | 5-tool MCP surface over stdio | done |

Quality gate: 57 unit tests pass; ruff and mypy (strict) clean across all source files.

## Backend verification matrix

| Backend | Installed | Read-only | Safe-edit (worktree write) | Native usage |
|---------|-----------|-----------|----------------------------|--------------|
| OpenCode | yes | verified | verified | verified (tokens + cost) |
| Cursor | yes | verified | verified | n/a by design (Admin API only) |
| Codex | yes | blocked* | blocked* | best-effort |
| Antigravity | yes | verified (reply) | not yet** | none |

\* Codex account is usage-limited until ~2026-07-18; only the failure path is verified.
\*\* Antigravity headless writes divert to `~/.gemini/antigravity-cli/scratch` under an
untrusted workspace (`--add-dir` does not fix it). Needs a PTY / workspace-trust workaround.

## Roadmap

### Tier 1 — close the core loop
1. **`collect_run` / `integrate`** — surface a run's diff + changed files, and merge a worktree
   branch back to base with conflict handling; expose both over MCP. (README already lists these
   tools — implement to match.)
2. **Parallel spawn + concurrency cap** — run N agents concurrently with a max-concurrency limit
   and a non-blocking spawn/poll mode (`Fleet.run` is sequential today).
3. **Skills layer** — `.claude/skills/marshal-*` playbooks teaching a driver to decompose →
   spawn → monitor → integrate.

### Tier 2 — robustness
4. Process-group kill on timeout (`os.killpg`).
5. Stamp `usage.duration_ms` (wall-clock around the run).
6. Antigravity PTY / workspace-trust for real worktree writes.

### Tier 3 — coverage
7. Cursor Admin-API usage (Team/Enterprise service-account key).
8. Codex live success-path re-verify after the account limit clears.
9. Richer MCP surface (diff via `get_run`/`collect_run`, `cancel_run`).

### Tier 4 — productization
PyPI publish, more backends (Gemini), then **Chauffeur** (the future product built on Marshal,
see [`chauffeur-future.md`](chauffeur-future.md)).
