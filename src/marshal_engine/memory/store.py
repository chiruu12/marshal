"""Cognee-backed memory store for fleet run recall and remember."""

from __future__ import annotations

import asyncio
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from .config import MemoryConfig

if TYPE_CHECKING:
    from marshal_engine.state import RunRecord

logger = logging.getLogger(__name__)

_DIFF_TRUNCATE = 4000


def _find_repo_root() -> Path:
    env = os.environ.get("MARSHAL_REPO")
    if env:
        return Path(env)
    cwd = Path.cwd()
    for candidate in (cwd, *cwd.parents):
        if (candidate / ".git").exists():
            return candidate
    return cwd


def _resolve_data_dir(config: MemoryConfig) -> Path:
    if config.data_dir:
        return Path(config.data_dir)
    return _find_repo_root() / ".marshal" / "memory"


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _format_run_document(record: RunRecord, diff: str | None = None) -> str:
    """Build a readable markdown document for a completed fleet run."""
    lines = [
        "# Fleet run",
        "",
        f"**Run:** {record.run_id}",
        f"**Task:** {record.task_id}",
        f"**Status:** {record.status}",
        f"**Backend:** {record.backend}",
    ]
    if record.client:
        lines.append(f"**Client:** {record.client}")
    if record.model:
        lines.append(f"**Model:** {record.model}")
    if record.branch:
        lines.append(f"**Branch:** {record.branch}")
    if record.worktree:
        lines.append(f"**Worktree:** {record.worktree}")
    if record.source:
        lines.append(f"**Cost source:** {record.source}")
    lines.extend(
        [
            f"**Cost (USD):** {record.cost_usd:.6f}",
            f"**Duration (ms):** {record.duration_ms}",
            f"**Tokens:** in={record.input_tokens} out={record.output_tokens}",
        ]
    )
    if record.error:
        lines.extend(["", "## Error", record.error])
    lines.extend(["", "## Agent output", record.text or "(no agent text)"])
    if diff:
        lines.extend(["", "## Files changed", _truncate(diff, _DIFF_TRUNCATE)])
    return "\n".join(lines)


def _format_note_document(text: str) -> str:
    """Build a readable markdown document for a freeform memory note."""
    return f"# Note\n\n{text}"


def _flatten_recall_strings(value: Any) -> list[str]:
    """Collect non-empty string fragments from nested recall payload values."""
    if value is None:
        return []
    if isinstance(value, str):
        stripped = value.strip()
        return [stripped] if stripped else []
    if isinstance(value, list):
        out: list[str] = []
        for elem in value:
            out.extend(_flatten_recall_strings(elem))
        return out
    return []


def _extract_recall_part(item: Any) -> str:
    if isinstance(item, dict):
        if "search_result" in item:
            parts = _flatten_recall_strings(item["search_result"])
            if parts:
                return "\n".join(parts)
        for key in ("answer", "text"):
            val = item.get(key)
            if val:
                parts = _flatten_recall_strings(val)
                if parts:
                    return "\n".join(parts)
                stripped = str(val).strip()
                if stripped:
                    return stripped
        return str(item)
    if hasattr(item, "text") and item.text:
        return str(item.text).strip()
    if hasattr(item, "answer") and item.answer:
        return str(item.answer).strip()
    if isinstance(item, str):
        return item.strip()
    return str(item).strip()


def _format_recall(results: list[Any], max_chars: int) -> str:
    """Format Cognee search results into a prompt-injectable snippet."""
    parts: list[str] = []
    seen: set[str] = set()
    for item in results:
        part = _extract_recall_part(item)
        if part and part not in seen:
            seen.add(part)
            parts.append(part)
    text = "\n\n".join(parts)
    return _truncate(text, max_chars)


def _repo_name(record: RunRecord, repo: str | None) -> str:
    if repo:
        return repo
    if record.worktree:
        return Path(record.worktree).name
    return "default"


class CogneeMemory:
    """Best-effort Cognee wrapper for remembering and recalling fleet runs."""

    def __init__(self, config: MemoryConfig) -> None:
        self._config = config
        self._cognee: Any | None = None
        self._search_type: Any | None = None
        self._configured = False

    def _ensure_cognee(self) -> tuple[Any, Any]:
        if self._cognee is not None and self._search_type is not None:
            return self._cognee, self._search_type
        try:
            import cognee
            from cognee import SearchType
        except ImportError as exc:
            raise RuntimeError(
                "Marshal Recall requires Cognee. Install with: pip install 'marshal[memory]'"
            ) from exc
        if not self._configured:
            self._apply_cognee_config(cognee)
            self._configured = True
        self._cognee = cognee
        self._search_type = SearchType
        return cognee, SearchType

    def _apply_cognee_config(self, cognee: Any) -> None:
        root = _resolve_data_dir(self._config)
        root.mkdir(parents=True, exist_ok=True)
        cognee.config.system_root_directory(str(root / "system"))
        cognee.config.data_root_directory(str(root / "data"))

        llm: dict[str, str] = {}
        if self._config.llm_provider:
            llm["llm_provider"] = self._config.llm_provider
        if self._config.llm_model:
            llm["llm_model"] = self._config.llm_model
        if self._config.llm_endpoint:
            llm["llm_endpoint"] = self._config.llm_endpoint
        # Prefer env (LLM_API_KEY) over inline YAML. Inline memory.llm_api_key is deprecated —
        # doctor warns when it is set in fleet.config.yaml.
        api_key = self._config.llm_api_key or os.environ.get("LLM_API_KEY")
        if api_key:
            llm["llm_api_key"] = api_key
        # OpenAI-compatible LLM endpoints (e.g. EastRouter) need llm_model prefixed
        # with "openai/<model>" so litellm routes correctly - set via llm_model.
        if llm:
            cognee.config.set_llm_config(llm)

        emb: dict[str, str] = {}
        if self._config.embedding_provider:
            emb["embedding_provider"] = self._config.embedding_provider
        if self._config.embedding_model:
            emb["embedding_model"] = self._config.embedding_model
        elif self._config.embedding_provider == "fastembed":
            emb["embedding_model"] = "sentence-transformers/all-MiniLM-L6-v2"
        if emb:
            cognee.config.set_embedding_config(emb)

    async def remember(
        self,
        record: RunRecord,
        diff: str | None = None,
        repo: str | None = None,
    ) -> None:
        if not self._config.enabled or not self._config.remember_enabled:
            return
        try:
            cognee, _ = self._ensure_cognee()
            dataset = _repo_name(record, repo)
            doc = _format_run_document(record, diff)
            node_set = [
                f"client:{record.client}" if record.client else "client:unknown",
                f"status:{record.status}",
                f"task:{record.task_id}",
                "fleet-run",
            ]
            await cognee.add(doc, dataset_name=dataset, node_set=node_set)
            await cognee.cognify(
                datasets=dataset,
                run_in_background=self._config.remember_in_background,
            )
        except Exception:
            logger.exception("marshal recall: remember failed for run %s", record.run_id)

    async def remember_note(
        self,
        text: str,
        *,
        repo: str | None = None,
        tags: list[str] | None = None,
    ) -> None:
        if not self._config.enabled or not self._config.remember_enabled:
            return
        try:
            cognee, _ = self._ensure_cognee()
            dataset = repo or "default"
            doc = _format_note_document(text)
            node_set = ["note", *(tags or [])]
            await cognee.add(doc, dataset_name=dataset, node_set=node_set)
            await cognee.cognify(
                datasets=dataset,
                run_in_background=self._config.remember_in_background,
            )
        except Exception:
            logger.exception("marshal recall: remember_note failed for repo %s", repo or "default")

    async def recall(self, goal: str, repo: str, top_k: int | None = None) -> str:
        if not self._config.enabled or not self._config.recall_enabled:
            return ""
        try:
            cognee, SearchType = self._ensure_cognee()
            results = await cognee.search(
                query_text=goal,
                query_type=SearchType.GRAPH_COMPLETION,
                datasets=repo,
                top_k=top_k or self._config.recall_top_k,
            )
            if not results:
                return ""
            return _format_recall(list(results), self._config.recall_max_chars)
        except Exception:
            logger.exception("marshal recall: recall failed for repo %s", repo)
            return ""

    async def improve(self, repo: str) -> None:
        if not self._config.enabled:
            return
        try:
            cognee, _ = self._ensure_cognee()
            await cognee.memify(dataset=repo)
        except Exception:
            logger.exception("marshal recall: improve failed for repo %s", repo)

    async def forget(self, repo: str | None = None, *, everything: bool = False) -> None:
        if not self._config.enabled:
            return
        try:
            cognee, _ = self._ensure_cognee()
            if everything:
                await cognee.forget(everything=True)
            elif repo is not None:
                await cognee.forget(dataset=repo)
        except Exception:
            logger.exception("marshal recall: forget failed")

    def remember_sync(
        self,
        record: RunRecord,
        diff: str | None = None,
        repo: str | None = None,
    ) -> None:
        if not self._config.enabled or not self._config.remember_enabled:
            return
        _run_async(self.remember(record, diff=diff, repo=repo))

    def remember_note_sync(
        self,
        text: str,
        *,
        repo: str | None = None,
        tags: list[str] | None = None,
    ) -> None:
        if not self._config.enabled or not self._config.remember_enabled:
            return
        _run_async(self.remember_note(text, repo=repo, tags=tags))

    def recall_sync(self, goal: str, repo: str, top_k: int | None = None) -> str:
        if not self._config.enabled or not self._config.recall_enabled:
            return ""
        return cast(str, _run_async(self.recall(goal, repo, top_k=top_k)))


def _run_async(coro: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()
