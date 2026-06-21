# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Marshal is **pre-1.0**: minor
versions may include breaking API changes until 1.0.

## [Unreleased]

## [0.0.1]

First tagged release: the V1 vertical slice — engine -> service -> CLI -> MCP.

### Added
- **Engine** for driving headless coding agents in isolated git worktrees, off one base class
  (`CodingAgentBackend`) with a shared safe run loop: hard external timeout, no stdin, and a
  process-group kill on timeout.
- **Four backend adapters:** Cursor, OpenCode, Codex, and Google Antigravity.
- **MCP server** exposing an 11-tool surface: `list_clients`, `run_agent`, `run_many`, `spawn`,
  `benchmark`, `report`, `get_run`, `collect_run`, `integrate`, `status`, `usage`.
- **Merge-back workflow:** `collect_run` (read-only diff review) and `integrate` (explicit merge into
  the current branch); the main branch is untouched until integrate.
- **Per-provider usage tracking:** an append-only ledger (`usage/events.jsonl`) of facts (tokens /
  cost / duration / source) with interpretation derived on read. Cost is tagged by source
  (native / estimated / unavailable) and never fabricated as `$0`.
- **Capped parallel `run_many`** and **non-blocking `spawn`** for background runs.
- **Measured savings benchmark:** `benchmark` runs one goal through N strategies and `report`
  derives a source-honest cost / latency / outcome comparison; "cheapest" ranks only strategies with
  a known cost.
- **`marshal doctor`** preflight CLI command, plus `backends`, `status`, `usage`, and `mcp`.
- **Driver Skills:** `marshal-orchestrate` and `marshal-benchmark`.
- **Config** via `fleet.config.yaml` (clients = named backend instances) with an example template.

[Unreleased]: https://github.com/chiruu12/marshal/compare/v0.0.1...HEAD
[0.0.1]: https://github.com/chiruu12/marshal/releases/tag/v0.0.1
