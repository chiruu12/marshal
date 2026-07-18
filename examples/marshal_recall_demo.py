"""Marshal Recall demo: seed past fleet runs, enrich the graph, recall for a new task.

Required environment (Cognee / LiteLLM):
  LLM_API_KEY        API key for the recall LLM (required)
  LLM_PROVIDER       LiteLLM provider name (default: openai)
  LLM_MODEL          Model id; for OpenAI-compatible endpoints (e.g. EastRouter)
                     prefix with ``openai/<model>`` so LiteLLM routes correctly
  LLM_ENDPOINT       Optional custom API base URL
  EMBEDDING_PROVIDER Embedding backend (default: fastembed for local embeddings)
  EMBEDDING_MODEL    Optional embedding model override

Install: pip install 'marshal[memory,fastembed]'  (fastembed for local embeddings)

Run from repo root:

  export LLM_API_KEY=...
  uv run python examples/marshal_recall_demo.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

from marshal_engine.memory import CogneeMemory, MemoryConfig
from marshal_engine.service import _WORKER_PREAMBLE
from marshal_engine.state import RunRecord

REPO_DATASET = "marshal-recall-demo"

# Realistic prior runs on the same repo (distinct client/model/status for node_set tags).
PRIOR_RUNS: list[tuple[RunRecord, str | None]] = [
    (
        RunRecord(
            run_id="run-auth-fix-001",
            task_id="fix-login-null-check",
            backend="opencode",
            status="succeeded",
            client="implementer",
            model="opencode-go/glm-5.2",
            branch="marshal/fix-login-null-check",
            cost_usd=0.0042,
            input_tokens=8200,
            output_tokens=1100,
            duration_ms=94_000,
            source="native",
            text=(
                "Added a guard in auth/login.py so session lookup returns 401 when the user "
                "record is missing instead of raising AttributeError on None.email. Added "
                "test_login_missing_user_returns_401 in tests/test_auth_login.py."
            ),
        ),
        "auth/login.py | tests/test_auth_login.py\n+ if user is None: raise HTTPException(401)",
    ),
    (
        RunRecord(
            run_id="run-async-flake-002",
            task_id="stabilize-async-tests",
            backend="cursor",
            status="failed",
            client="reviewer",
            model="cursor-small",
            branch="marshal/async-test-flake",
            cost_usd=0.0,
            input_tokens=0,
            output_tokens=0,
            duration_ms=612_000,
            source="unavailable",
            error="pytest failed: test_refresh_token_async left coroutine unawaited",
            text=(
                "Tests in tests/test_session_refresh.py failed intermittently. "
                "test_refresh_token_async calls refresh_session() without await; "
                "under load the event loop warns and the assertion races."
            ),
        ),
        "tests/test_session_refresh.py\n- refresh_session(user_id)\n+ await refresh_session(user_id)",
    ),
    (
        RunRecord(
            run_id="run-module-pattern-003",
            task_id="refactor-auth-services",
            backend="claude-code",
            status="succeeded",
            client="planner",
            model="claude-sonnet-4-6",
            branch="marshal/auth-service-layout",
            cost_usd=0.18,
            input_tokens=14_200,
            output_tokens=3200,
            duration_ms=128_000,
            source="native",
            text=(
                "Moved token helpers into auth/services/session_tokens.py and kept route handlers "
                "thin. Pattern: routes validate input, services own business logic, models stay "
                "in auth/models. Re-export public helpers from auth/services/__init__.py."
            ),
        ),
        "auth/services/session_tokens.py (new) | auth/login.py (imports updated)",
    ),
]

NEW_GOAL = (
    "I need to touch the login/session token code. What should I know from earlier fleet work?"
)


def _memory_config_from_env(data_dir: Path) -> MemoryConfig:
    return MemoryConfig(
        enabled=True,
        recall_enabled=True,
        remember_enabled=True,
        remember_in_background=False,
        data_dir=str(data_dir),
        llm_api_key=os.environ.get("LLM_API_KEY"),
        llm_provider=os.environ.get("LLM_PROVIDER", "openai"),
        llm_model=os.environ.get("LLM_MODEL"),
        llm_endpoint=os.environ.get("LLM_ENDPOINT"),
        embedding_provider=os.environ.get("EMBEDDING_PROVIDER", "fastembed"),
        embedding_model=os.environ.get("EMBEDDING_MODEL"),
    )


def _env_ready() -> bool:
    if not os.environ.get("LLM_API_KEY"):
        print(
            "Marshal Recall demo needs LLM_API_KEY set.\n"
            "  export LLM_API_KEY=your-key\n"
            "Optional: LLM_PROVIDER (default openai), LLM_MODEL, LLM_ENDPOINT,\n"
            "          EMBEDDING_PROVIDER (default fastembed), EMBEDDING_MODEL.\n"
            "For OpenAI-compatible endpoints (e.g. EastRouter), set LLM_MODEL to openai/<model>."
        )
        return False
    return True


def _compose_goal_preview(goal: str, recalled: str) -> str:
    """Same memory injection shape MarshalService._compose_goal uses (worker context omitted)."""
    parts = [_WORKER_PREAMBLE]
    if recalled:
        parts.append(f"## Memory from past runs\n\n{recalled}")
    parts.append(goal)
    return "\n\n".join(parts)


async def _run_demo(memory: CogneeMemory) -> None:
    print("=" * 72)
    print("Marshal Recall demo")
    print("=" * 72)
    print()
    print(f"Dataset (repo partition): {REPO_DATASET!r}")
    print("Seeding three prior fleet runs into memory...")
    print()

    for record, diff in PRIOR_RUNS:
        print(f"  remember  {record.run_id}  status={record.status}  client={record.client}")
        await memory.remember(record, diff=diff, repo=REPO_DATASET)
    print()
    print("Prior runs stored. Running improve() to enrich the knowledge graph...")
    await memory.improve(REPO_DATASET)
    print("Graph enriched (memify complete).")
    print()
    print("-" * 72)
    print("RECALL for a new related task")
    print("-" * 72)
    print(f"Query: {NEW_GOAL!r}")
    print()

    recalled = await memory.recall(NEW_GOAL, REPO_DATASET)
    if recalled:
        print("Recalled snippet:")
        print()
        print(recalled)
    else:
        print("(no relevant memory returned; try a different LLM_MODEL or wait for cognify to finish)")
    print()
    print("-" * 72)
    print("How this reaches the next fleet agent")
    print("-" * 72)
    print("Marshal prepends recalled learnings when composing the worker goal:")
    print()
    preview = _compose_goal_preview(NEW_GOAL, recalled)
    print(preview[: min(len(preview), 2400)])
    if len(preview) > 2400:
        print("\n... (truncated for display)")
    print()
    print("-" * 72)
    print("Optional: forget this dataset")
    print("-" * 72)
    await memory.forget(REPO_DATASET)
    print(f"forget(dataset={REPO_DATASET!r}) complete.")
    after_forget = await memory.recall(NEW_GOAL, REPO_DATASET)
    if after_forget:
        print("Note: recall still returned text (graph may need a moment to settle).")
    else:
        print("Recall is now empty for this dataset.")


def main() -> int:
    if not _env_ready():
        return 1

    try:
        import cognee  # noqa: F401
    except ImportError:
        print(
            "Marshal Recall requires Cognee. Install with:\n"
            "  pip install 'marshal[memory,fastembed]'"
        )
        return 1

    with tempfile.TemporaryDirectory(prefix="marshal-recall-demo-") as tmp:
        data_dir = Path(tmp)
        memory = CogneeMemory(_memory_config_from_env(data_dir))
        try:
            asyncio.run(_run_demo(memory))
        except RuntimeError as exc:
            msg = str(exc)
            if "marshal[memory]" in msg.lower() or "cognee" in msg.lower():
                print(f"Marshal Recall setup error: {msg}")
                return 1
            raise
        except Exception as exc:
            err = str(exc).lower()
            if "api" in err and "key" in err:
                print(
                    "LLM call failed (check LLM_API_KEY and LLM_MODEL).\n"
                    "For OpenAI-compatible endpoints, prefix LLM_MODEL with openai/<model>."
                )
                return 1
            print(f"Demo failed: {exc}")
            return 1

    print()
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
