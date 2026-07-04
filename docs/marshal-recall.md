# Marshal Recall (persistent fleet memory)

Marshal Recall is a Cognee-backed memory layer so coding agents in your fleet carry learnings across runs instead of starting cold every time.

## What it does

After each fleet run completes, Marshal **remembers** the outcome: task description, repo, client and model, status, agent summary, and a truncated diff. Before the next run starts, Marshal **recalls** relevant past learnings for the new goal and injects them into the worker context.

Two integration points drive this automatically:

1. **Run-complete hook**: when a run finishes, its record is written into the memory graph.
2. **Goal composition**: when a worker goal is built, a recall query runs against the repo dataset and any hit is prepended under a "Memory from past runs" heading.

You can also query, enrich, or wipe memory explicitly via CLI or MCP.

## Partitioning

| Concept | Maps to |
|---|---|
| **Dataset** | One git repo (workspace) |
| **node_set tags** | `client:...`, `status:...`, `task:...`, plus `fleet-run` |
| **Session** | Task group within a run (Cognee session metadata) |

Runs from different clients, statuses, and tasks remain distinguishable in the graph while sharing one repo-level dataset.

## Operations

| Operation | When | Purpose |
|---|---|---|
| **remember** | After each run (automatic) | Ingest run document + diff into the graph |
| **recall** | Before each run (automatic) | Retrieve a short snippet relevant to the new goal |
| **improve** | Manual / scheduled | Run Cognee `memify` to enrich relationships in the dataset |
| **forget** | Manual | Drop one repo dataset or wipe all memory |

## Configuration

### fleet.config.yaml

Recall is **off by default**. Enable it with a `memory:` block:

```yaml
memory:
  enabled: true
  recall_enabled: true
  remember_enabled: true
  recall_top_k: 5
  recall_max_chars: 1200
  remember_in_background: true   # false = cognify blocks until the graph is queryable
  # data_dir: /path/to/memory   # default: <repo>/.marshal/memory
  # llm_provider: openai
  # llm_model: gpt-4o-mini
  # llm_endpoint: https://...
  # llm_api_key: sk-...         # or set LLM_API_KEY in the environment
  # embedding_provider: fastembed
  # embedding_model: sentence-transformers/all-MiniLM-L6-v2
```

Fleet YAML values override environment defaults where both are set.

### Cognee environment

Marshal passes LLM and embedding settings into Cognee. Common variables:

| Variable | Role |
|---|---|
| `LLM_API_KEY` | API key for graph completion during recall |
| `LLM_PROVIDER` | LiteLLM provider (default `openai`) |
| `LLM_MODEL` | Model id; for OpenAI-compatible gateways (e.g. EastRouter) use `openai/<model>` |
| `LLM_ENDPOINT` | Optional custom base URL |
| `EMBEDDING_PROVIDER` | Default `fastembed` (local, no cloud key) |
| `EMBEDDING_MODEL` | Optional override when not using the fastembed default |

### Install

```bash
pip install 'marshal[memory]'           # Cognee backend
pip install 'marshal[memory,fastembed]' # plus local embeddings (recommended)
```

## CLI

```bash
marshal memory query "login session token pitfalls"
marshal memory stats
marshal memory improve
marshal memory forget          # this repo's dataset
marshal memory forget --all    # every dataset
```

Use `--repo` and `--config` like other `marshal` subcommands when not in the project root.

## MCP tools

| Tool | Description |
|---|---|
| `memory_query` | Recall a snippet for a natural-language query in the selected workspace |
| `memory_add` | Store a freeform note into the workspace's shared memory graph, recallable via `memory_query` |
| `memory_stats` | Enabled flags, data directory, recall limits, Cognee install status |

Both accept an optional `workspace` argument (multi-repo servers).

## Run the demo

A narrated, self-contained script seeds three realistic prior runs, enriches the graph, recalls for a new login-related task, and shows how injection reaches the next agent:

```bash
export LLM_API_KEY=...
uv run python examples/marshal_recall_demo.py
```

See [`examples/marshal_recall_demo.py`](../examples/marshal_recall_demo.py) for required environment variables and narration output.
