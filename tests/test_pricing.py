"""Tests for the token -> cost pricing module (the ESTIMATED path)."""

from __future__ import annotations

from pathlib import Path

import pytest

from marshal_engine.pricing import ModelPrice, PriceTable, PricingError


def test_estimate_prices_tokens() -> None:
    t = PriceTable({"m": ModelPrice(input_per_mtok=10.0, output_per_mtok=20.0)})
    # 1M input @ $10 + 0.5M output @ $20 = 10 + 10 = 20
    assert t.estimate("m", input_tokens=1_000_000, output_tokens=500_000) == 20.0


def test_estimate_unpriced_model_returns_none() -> None:
    t = PriceTable({"m": ModelPrice(1.0, 1.0)})
    assert t.estimate("other", input_tokens=1000, output_tokens=1000) is None  # missing -> unpriced
    assert t.estimate(None, input_tokens=1000, output_tokens=0) is None         # no model -> unpriced


def test_estimate_includes_cache_tokens() -> None:
    t = PriceTable({"m": ModelPrice(input_per_mtok=0.0, output_per_mtok=0.0, cache_read_per_mtok=2.0)})
    assert t.estimate("m", input_tokens=0, output_tokens=0, cache_read_tokens=1_000_000) == 2.0


def test_load_default_table() -> None:
    assert isinstance(PriceTable.load(), PriceTable)  # shipped default exists and parses


def test_load_malformed_raises(tmp_path: Path) -> None:
    bad = tmp_path / "p.yaml"
    bad.write_text("models: [not, a, mapping]\n")
    with pytest.raises(PricingError):
        PriceTable.load(bad)


def test_load_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(PricingError):
        PriceTable.load(tmp_path / "nope.yaml")
