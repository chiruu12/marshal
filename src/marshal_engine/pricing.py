"""Token -> cost pricing: the ESTIMATED path.

A ``model -> price`` table converts token counts into a cost estimate for backends that report
tokens but not cost. Pricing lives in this ONE module so backend adapters stay config-free
(see the engine/report split in docs/internal/plans/phase1-cost-proof.md). Prices are USD per million
tokens. A model missing from the table is **unpriced** (``estimate`` returns ``None``) — never
silently ``$0``. The table is data the user owns and should keep current; an estimate reflects the
table's prices at the moment of the run.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict

DEFAULT_PRICES_PATH = Path(__file__).parent / "data" / "prices.yaml"


class PricingError(ValueError):
    """The price table is missing or malformed."""


class ModelPrice(BaseModel):
    """USD per million tokens for one model."""

    model_config = ConfigDict(frozen=True)

    input_per_mtok: float
    output_per_mtok: float
    cache_read_per_mtok: float = 0.0


class PriceTable:
    """Model -> price lookup that estimates cost from tokens, or ``None`` when unpriced."""

    def __init__(self, prices: dict[str, ModelPrice]) -> None:
        self._prices = dict(prices)

    @classmethod
    def load(cls, path: Path | str | None = None) -> PriceTable:
        """Load a YAML price table. Raises PricingError on a missing/malformed file.

        Callers that must not fail a run (e.g. the fleet) catch PricingError and fall back to an
        empty table (everything unpriced) rather than crashing.
        """
        src = Path(path) if path is not None else DEFAULT_PRICES_PATH
        try:
            raw: Any = yaml.safe_load(src.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise PricingError(f"price table not found: {src}") from exc
        data = raw if isinstance(raw, dict) else {}
        models = data.get("models") or {}
        if not isinstance(models, dict):
            raise PricingError(f"price table {src}: 'models' must be a mapping")
        prices: dict[str, ModelPrice] = {}
        for key, spec in models.items():
            if not isinstance(spec, dict):
                raise PricingError(f"price table {src}: entry {key!r} must be a mapping")
            try:
                prices[str(key)] = ModelPrice(
                    input_per_mtok=float(spec["input_per_mtok"]),
                    output_per_mtok=float(spec["output_per_mtok"]),
                    cache_read_per_mtok=float(spec.get("cache_read_per_mtok", 0.0)),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise PricingError(f"price table {src}: bad entry {key!r}: {exc}") from exc
        return cls(prices)

    def has(self, model: str | None) -> bool:
        return model is not None and model in self._prices

    def estimate(
        self,
        model: str | None,
        *,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int = 0,
    ) -> float | None:
        """USD cost for these tokens under ``model``, or ``None`` if the model is unpriced."""
        price = self._prices.get(model) if model is not None else None
        if price is None:
            return None  # unpriced — never fabricate a cost
        return round(
            input_tokens / 1_000_000 * price.input_per_mtok
            + output_tokens / 1_000_000 * price.output_per_mtok
            + cache_read_tokens / 1_000_000 * price.cache_read_per_mtok,
            6,
        )
