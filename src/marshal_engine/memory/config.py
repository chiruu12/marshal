"""Marshal Recall memory configuration."""

from __future__ import annotations

from pydantic import BaseModel


class MemoryConfig(BaseModel):
    """Optional Cognee-backed memory layer for fleet runs."""

    enabled: bool = False
    recall_enabled: bool = True
    remember_enabled: bool = True
    recall_top_k: int = 5
    recall_max_chars: int = 1200
    remember_in_background: bool = True
    # Cognee local DB root; when unset, defaults to ``<repo>/.marshal/memory``.
    data_dir: str | None = None
    llm_provider: str | None = None
    llm_model: str | None = None
    llm_endpoint: str | None = None
    # Deprecated: prefer exporting LLM_API_KEY. Inline keys land in YAML backups more easily;
    # the store still honors this field, then falls back to the env var.
    llm_api_key: str | None = None
    embedding_provider: str | None = "fastembed"
    embedding_model: str | None = None

    @classmethod
    def disabled(cls) -> MemoryConfig:
        """An all-off config (memory layer present but inactive)."""
        return cls(enabled=False, recall_enabled=False, remember_enabled=False)
