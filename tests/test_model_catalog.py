"""The shipped model catalog: it parses, it stays honest, and staleness is enforced."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from marshal_engine.core.catalog import CATALOG_PATH, load_catalog, shipped_models
from marshal_engine.core.config import (
    MODEL_CATEGORIES,
    MODEL_EVIDENCE,
    MODEL_WEIGHTS,
    ConfigError,
    _parse_models,
)
from marshal_engine.interfaces.drift import FAIL, OK, detect_drift


def test_the_shipped_catalog_parses_and_is_not_empty() -> None:
    catalog = load_catalog()
    assert catalog.models, "the shipped catalog is the default answer to 'which model' - empty is a bug"
    assert catalog.cadence
    assert catalog.stale_after_days >= 1


def test_every_shipped_opinion_carries_its_provenance() -> None:
    """The whole design: an opinion without a date and an evidence tier is an unfalsifiable claim."""
    for m in load_catalog().models:
        if not (m.review or m.categories or m.weight):
            continue
        assert m.reviewed_on is not None, f"{m.id}: an opinion with no review date cannot go stale"
        assert m.evidence in MODEL_EVIDENCE, f"{m.id}: evidence must say how strongly it is backed"


def test_the_category_vocabulary_in_the_file_matches_the_one_in_code() -> None:
    """Code owns the closed set; the file owns the prose. A test is what holds them together."""
    assert set(load_catalog().categories) == set(MODEL_CATEGORIES)


def test_every_shipped_model_names_a_real_backend() -> None:
    from marshal_engine.orchestration.registry import default_backends

    known = set(default_backends())
    for m in load_catalog().models:
        assert set(m.backends) <= known, f"{m.id} names a backend that does not exist"


def test_an_entry_never_reviewed_is_stale_rather_than_fresh() -> None:
    """REGRESSION: defaulting a missing `reviewed_on` to fresh would hide every undated opinion."""
    models = _parse_models([{"id": "m", "backends": ["cursor"], "review": "great", "weight": "heavy"}])
    catalog = load_catalog()
    catalog.models = models
    stale = catalog.stale(date(2026, 1, 1))
    assert [reason for _, reason in stale] == ["never reviewed"]


def test_staleness_trips_only_past_the_window() -> None:
    models = _parse_models(
        [{"id": "m", "backends": ["cursor"], "review": "x", "reviewed_on": "2026-01-01"}]
    )
    catalog = load_catalog()
    catalog.models = models
    catalog.stale_after_days = 14
    assert catalog.stale(date(2026, 1, 15)) == []
    assert catalog.stale(date(2026, 1, 16))[0][1] == "last reviewed 15 days ago"


def test_drift_fails_on_an_overdue_review(tmp_path: Path) -> None:
    """The cadence is only real if something enforces it."""
    path = tmp_path / "models.yaml"
    path.write_text(
        "stale_after_days: 14\nreview_cadence: weekly on Sunday\n"
        "models:\n  - id: m\n    backends: [cursor]\n    review: stale take\n"
        "    reviewed_on: 2026-01-01\n",
        encoding="utf-8",
    )
    report = detect_drift(backends={}, today=date(2026, 6, 1), catalog_path=path)
    finding = next(f for f in report.findings if f.kind == "reviews")
    assert finding.status == FAIL
    assert "m (last reviewed" in finding.detail
    assert not report.ok


def test_drift_passes_on_a_fresh_catalog(tmp_path: Path) -> None:
    """The anti-blanket control: the check must not fail every catalog it is handed."""
    path = tmp_path / "models.yaml"
    path.write_text(
        "stale_after_days: 14\nmodels:\n  - id: m\n    backends: [cursor]\n"
        "    review: fresh take\n    reviewed_on: 2026-05-30\n",
        encoding="utf-8",
    )
    report = detect_drift(backends={}, today=date(2026, 6, 1), catalog_path=path)
    assert next(f for f in report.findings if f.kind == "reviews").status == OK


def test_the_shipped_catalog_is_reviewed_on_a_real_clock() -> None:
    """Ships green today; goes red once the weekly pass is genuinely overdue. That is the point."""
    catalog = load_catalog(CATALOG_PATH)
    assert catalog.stale(max(m.reviewed_on for m in catalog.models if m.reviewed_on)) == []


@pytest.mark.parametrize(
    "entry,fragment",
    [
        ({"id": "m", "backends": ["cursor"], "weight": "hevy"}, "weight"),
        ({"id": "m", "backends": ["cursor"], "evidence": "vibes"}, "evidence"),
        ({"id": "m", "backends": ["cursor"], "categories": ["cheapest"]}, "categories"),
        ({"id": "m", "backends": ["cursor"], "reviewed_on": "last tuesday"}, "reviewed_on"),
    ],
)
def test_a_typo_in_a_closed_field_fails_fast(entry: dict, fragment: str) -> None:
    """A `weight: hevy` that parsed to "" would drop the entry from every filter, silently."""
    with pytest.raises(ConfigError, match=fragment):
        _parse_models([entry])


def test_stale_after_days_rejects_a_bool() -> None:
    """REGRESSION: `bool` subclasses `int`, so `true` would parse as 1 day - permanent staleness."""
    path = Path(__file__).parent / "_bool.yaml"
    path.write_text("stale_after_days: true\nmodels: []\n", encoding="utf-8")
    try:
        with pytest.raises(ConfigError, match="positive integer"):
            load_catalog(path)
    finally:
        path.unlink()


def test_a_damaged_catalog_degrades_instead_of_breaking_the_listing() -> None:
    assert isinstance(shipped_models(), list)


def test_weights_are_the_three_the_playbook_uses() -> None:
    assert set(MODEL_WEIGHTS) == {"heavy", "standard", "light"}


def test_a_future_review_date_is_stale_not_fresh() -> None:
    """REGRESSION: a future date has a negative age, which can never exceed the window.

    Left alone, one wrong digit opts an entry out of staleness - and the shipped catalog out of
    its drift failure - until the date arrives. The mechanism would be disabled by a typo.
    """
    catalog = load_catalog()
    catalog.models = _parse_models(
        [{"id": "m", "backends": ["cursor"], "review": "x", "reviewed_on": "2027-01-01"}]
    )
    assert catalog.stale(date(2026, 9, 8))[0][1].startswith("reviewed_on is in the future")


def test_the_service_uses_the_catalogs_own_staleness_window(tmp_path: Path) -> None:
    """REGRESSION: rebuilding the window from the default made it two records of one fact.

    Raising `stale_after_days` in the catalog would move what `marshal drift` fails on while
    `stale_reviews` kept answering from the constant, and the two surfaces would disagree.
    """
    from marshal_engine.core.catalog import load_shipped_catalog

    shipped = load_shipped_catalog()
    assert shipped is not None
    assert shipped.stale_after_days == load_catalog(CATALOG_PATH).stale_after_days


def test_json_output_honours_the_category_filter(tmp_path: Path) -> None:
    """REGRESSION: `--json` built its payload before the filter ran, so it returned everything -
    to the caller most likely to act on the result mechanically."""
    import json

    from marshal_engine.interfaces import cli

    cfg = tmp_path / "fleet.config.yaml"
    cfg.write_text("clients:\n  a:\n    backend: cursor\n", encoding="utf-8")
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        ret = cli.main(
            ["models", "--repo", str(tmp_path), "--config", str(cfg), "--json", "--category", "best"]
        )
    assert ret == 0
    ids = [m["id"] for m in json.loads(buf.getvalue())["models"]]
    assert ids and all("best" in m["categories"] for m in json.loads(buf.getvalue())["models"])
    assert len(ids) < len(load_catalog().models), "the filter must actually narrow the catalog"
