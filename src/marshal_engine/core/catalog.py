"""The model catalog Marshal ships - loading it, and telling you when it has gone stale.

`fleet.config.yaml` has always been able to declare a `models:` block, and almost no repo did:
the field was empty by default, so `marshal models` fell back to probing each installed CLI and
the driver got a bare list of ids with nothing to choose between them. This module supplies the
missing default - a curated catalog with a review, a weight and a set of categories per model.

**The failure mode this is built against.** A hand-maintained file of model recommendations is a
record that can disagree with reality, which is the defect class this codebase keeps meeting. It
is worse than no file, because confident prose reads as authority regardless of its age. So the
catalog carries `reviewed_on` and `evidence` on every entry, and `marshal drift` fails on a stale
one. The freshness of an opinion is a fact, and facts are checkable.

Nothing here feeds routing. Clients own backend+model; this is a catalogue you read.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import yaml

from .config import ConfigError, ModelSpec, _parse_models

#: The shipped catalog, alongside this module so it travels with the installed package.
CATALOG_PATH = Path(__file__).parent / "models.yaml"

#: Used when the catalog omits `stale_after_days`. Two missed weekly reviews: one skipped Sunday
#: is a normal week, two is a file nobody is maintaining - which is exactly when it starts doing
#: damage rather than none.
DEFAULT_STALE_AFTER_DAYS = 14


class ModelCatalogFile:
    """A parsed catalog: the entries, the category vocabulary, and the staleness rule."""

    def __init__(
        self,
        models: list[ModelSpec],
        categories: dict[str, str],
        stale_after_days: int,
        cadence: str,
    ) -> None:
        self.models = models
        self.categories = categories
        self.stale_after_days = stale_after_days
        self.cadence = cadence

    def in_category(self, category: str) -> list[ModelSpec]:
        """Entries tagged with `category`, in catalog order. Unknown category -> []."""
        return [m for m in self.models if category in m.categories]

    def stale(self, today: date) -> list[tuple[ModelSpec, str]]:
        """Entries overdue for review, each with the reason, worst first.

        `today` is a parameter rather than a `date.today()` call so this is pure and the staleness
        rule can be tested at a chosen date instead of only on the day the suite happens to run.
        An entry with no `reviewed_on` at all is stale by definition: never reviewed is not the
        same as reviewed recently, and defaulting it to fresh would hide every un-dated opinion.
        """
        out: list[tuple[ModelSpec, str]] = []
        for m in self.models:
            if m.reviewed_on is None:
                out.append((m, "never reviewed"))
                continue
            age = (today - m.reviewed_on).days
            if age < 0:
                # A date in the future is a typo or a review scheduled before it happened, and
                # either way its negative age can never exceed the window - so left alone it
                # suppresses this entry's staleness, and the shipped catalog's drift failure with
                # it, until the date arrives. The whole mechanism is opt-out by one wrong digit.
                out.append((m, f"reviewed_on is in the future ({m.reviewed_on.isoformat()})"))
                continue
            if age > self.stale_after_days:
                out.append((m, f"last reviewed {age} days ago"))
        # Un-dated entries sort last by age but are the worst case, so they lead. Within each
        # group, oldest first - that is the order you would work the backlog in.
        return sorted(
            out,
            key=lambda pair: (pair[0].reviewed_on is not None, pair[0].reviewed_on or date.min),
        )


def load_catalog(path: Path | None = None) -> ModelCatalogFile:
    """Parse a catalog file. Raises `ConfigError` on anything malformed.

    Entries go through the config loader's own `models:` parser, so a repo-declared catalog and
    the shipped one are validated by exactly the same rules - there is no second, laxer path into
    the same type.
    """
    target = path or CATALOG_PATH
    try:
        raw: Any = yaml.safe_load(target.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read the model catalog at {target}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in the model catalog at {target}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{target}: the catalog must be a mapping, got {type(raw).__name__}")

    categories_raw = raw.get("categories") or {}
    if not isinstance(categories_raw, dict):
        raise ConfigError(f"{target}: categories must be a mapping of name -> description")

    stale_raw = raw.get("stale_after_days", DEFAULT_STALE_AFTER_DAYS)
    if not isinstance(stale_raw, int) or isinstance(stale_raw, bool) or stale_raw < 1:
        # `bool` is an `int` subclass, so `stale_after_days: true` would otherwise parse as 1 day
        # and put the whole catalog into permanent staleness.
        raise ConfigError(f"{target}: stale_after_days must be a positive integer, got {stale_raw!r}")

    return ModelCatalogFile(
        models=_parse_models(raw.get("models")),
        categories={str(k): str(v) for k, v in categories_raw.items()},
        stale_after_days=stale_raw,
        cadence=str(raw.get("review_cadence", "") or ""),
    )


def load_shipped_catalog() -> ModelCatalogFile | None:
    """The shipped catalog, or `None` if it cannot be read.

    Returns the whole catalog rather than only its entries so a caller inherits its staleness
    window too. Handing back the models alone forced every consumer to re-derive that window from
    the default constant, which is how one fact ends up with two records that can disagree.

    Degrades rather than raising: a broken catalog must not take down `marshal models` or the
    service, because this is a convenience listing and the caller still has the live probe. The
    suite holds the shipped file valid, so a failure here means a damaged install.
    """
    try:
        return load_catalog()
    except ConfigError:
        return None


def shipped_models() -> list[ModelSpec]:
    """The shipped catalog's entries, or `[]` if it cannot be read."""
    catalog = load_shipped_catalog()
    return catalog.models if catalog is not None else []
