"""``marshal drift`` - tell you when a backend CLI has moved out from under its adapter.

Marshal drives third-party CLIs it does not control, and the test suite cannot see them. Contract
tests pin the argv Marshal *builds*; nothing pins what a CLI still *accepts*. So a CLI can change
upstream and the whole suite stays green - which has now happened repeatedly to one backend alone:
Antigravity's ``-m`` alias was removed, its catalogue moved to effort-suffixed ids so every model
id Marshal shipped became invalid, and ``--print-timeout`` began capping runs at five minutes.
Every one of those was found by a user running into it, not by CI.

This module is the check that would have caught them. It asks each installed CLI what it is and
what it offers, and compares that with what the adapter records. Nothing here spawns an agent or
spends quota: every probe is a CLI answering a question about itself.

Two signals, deliberately at different severities:

* ``models`` **fails**. A model id in the adapter's own curated fallback that the live CLI no
  longer lists is a defect, not a notification - it means the path Marshal degrades to hands the
  caller an id guaranteed to fail the run. That is the exact shape of the Antigravity bug.
* ``models`` **warns** (and ``ok`` is false). An installed CLI whose models probe failed could not
  have its curated fallback verified - weaker than clean, not a confirmed lying fallback. Re-verify
  with a real run; do not bump the baseline from the warning alone.
* ``version`` **warns**. A CLI that is not the build the adapter was verified against is not
  broken; it is unverified. Some of these CLIs ship nightly, so a version finding that failed
  would be red every single day and read as noise within a week. It asks for a re-verify, and the
  observed line is printed so recording the new baseline is a one-line edit. Version drift alone
  leaves ``ok`` true.

Which is the whole design constraint: a drift check nobody reads catches nothing. Everything that
cannot be acted on is ``info``, and only a lying fallback is allowed to fail the command.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from pydantic import BaseModel

from ..backends.base import CodingAgentBackend
from ..core.types import ModelSource
from ..orchestration.registry import default_backends

OK = "ok"
INFO = "info"
WARN = "warn"
FAIL = "fail"

#: How many newly-offered model ids to name before summarising. A probed catalogue can run to
#: hundreds of ids (OpenCode lists every provider it can reach), and a finding that prints all of
#: them buries the one line that matters.
_MAX_NAMED_NEW = 5

#: A dotted numeric version token, e.g. ``1.1.15`` or ``2026.08.11`` (a trailing build suffix like
#: ``-e8db854`` is deliberately not part of the match; only the shape matters here).
_VERSION_TOKEN = re.compile(r"\d+\.\d+")


def _looks_like_a_version(line: str) -> bool:
    """True when a ``--version`` line reads as one version rather than a notice about versions.

    These CLIs self-update, and one caught mid-update answered ``--version`` with nothing but
    ``Updated 1.26.0 -> 1.28.1``. Warning on that is right - the build really did move - but
    offering it as the string to record as a baseline is not, and a baseline of a notice would
    then mismatch forever. Two version tokens on one line means the line is describing a change,
    not stating a version.
    """
    return len(_VERSION_TOKEN.findall(line)) == 1


class DriftFinding(BaseModel):
    """One observation about one backend. ``fix`` is shown only when ``status`` is not ``ok``."""

    backend: str
    kind: str
    status: str
    detail: str
    fix: str = ""


class DriftReport(BaseModel):
    """Per-backend findings plus a roll-up.

    ``ok`` is true only when nothing failed *and* every checked backend that can probe models
    had its catalogue verified. An installed CLI whose models probe failed is weaker than a
    clean check - that path is how a stale curated fallback reaches a run - so it is not ``ok``.
    Version drift alone still warns without failing ``ok`` (nightly CLIs would otherwise stay red).
    """

    findings: list[DriftFinding]
    checked: list[str]
    skipped: list[str]
    fails: int
    warns: int
    ok: bool


def _version_finding(name: str, backend: CodingAgentBackend, observed: str) -> DriftFinding:
    baseline = backend.verified_version
    cls = type(backend).__name__
    record = (
        f'record it as {cls}.verified_version = "{observed}"'
        if _looks_like_a_version(observed)
        else (
            f"re-run `{backend.binary} --version` before recording a baseline - this answer reads "
            "as a notice rather than a version"
        )
    )
    if baseline is None:
        return DriftFinding(
            backend=name,
            kind="version",
            status=INFO,
            detail=f"installed {observed}; no verified baseline recorded",
            fix=f"once verified, {record}",
        )
    if baseline == observed:
        return DriftFinding(
            backend=name,
            kind="version",
            status=OK,
            detail=f"installed {observed} (as verified)",
        )
    return DriftFinding(
        backend=name,
        kind="version",
        status=WARN,
        detail=f"installed {observed}; adapter verified against {baseline}",
        fix=f"re-verify this backend end to end, then {record}",
    )


def _models_finding(name: str, backend: CodingAgentBackend) -> DriftFinding:
    catalog = backend.available_models()
    if catalog.source is ModelSource.PROBE_FAILED:
        # Version answered, so the CLI is installed; the catalogue could not be verified. That is
        # weaker than a clean check - the curated fallback is exactly the path that fails at
        # runtime when upstream drops an id - and must not read as agreement.
        return DriftFinding(
            backend=name,
            kind="models",
            status=WARN,
            detail=(
                "CLI answered its version but the models probe failed; curated fallback was not "
                "verified against the live catalogue"
            ),
            fix=(
                "re-verify with a real run that pins a curated fallback id - do not bump the "
                "baseline from this warning alone"
            ),
        )
    if catalog.source is not ModelSource.PROBED:
        # STATIC (or UNAVAILABLE): this adapter has no live models probe, so there is nothing to
        # claim. Distinct from PROBE_FAILED above - the adapter said so via ModelSource.
        return DriftFinding(
            backend=name,
            kind="models",
            status=INFO,
            detail="live catalogue unavailable; the curated fallback cannot be checked",
        )
    live = set(catalog.models)
    if not backend.static_models:
        return DriftFinding(
            backend=name,
            kind="models",
            status=INFO,
            detail=f"{len(live)} model(s) offered; adapter ships no curated fallback to compare",
        )
    missing = [m for m in backend.static_models if m not in live]
    new = [m for m in catalog.models if m not in set(backend.static_models)]
    if missing:
        named = ", ".join(missing)
        return DriftFinding(
            backend=name,
            kind="models",
            status=FAIL,
            detail=(
                f"curated fallback names {len(missing)} id(s) the CLI no longer offers: {named}"
            ),
            fix=(
                f"update {type(backend).__name__}.static_models from a live "
                f"`{backend.binary} models` - a run that degrades to this list will fail"
            ),
        )
    checked = f"every curated id is still offered ({len(backend.static_models)} checked)"
    if not new:
        return DriftFinding(backend=name, kind="models", status=OK, detail=checked)
    # Naming the extras is only meaningful when the fallback plausibly tracks the catalogue. Some
    # adapters curate a shortlist of one or two ids out of every model the CLI can reach; for
    # those, "here are the hundreds you do not list" is not a finding, it is the design, and
    # printing it every run is how a check stops being read. So the extras are named only while
    # they stay within the size of the list itself, and otherwise collapse to a count on an
    # ``ok`` line.
    if len(new) > len(backend.static_models):
        return DriftFinding(
            backend=name,
            kind="models",
            status=OK,
            detail=f"{checked}; the CLI offers {len(new)} more the adapter does not curate",
        )
    shown = ", ".join(new[:_MAX_NAMED_NEW])
    more = "" if len(new) <= _MAX_NAMED_NEW else f", +{len(new) - _MAX_NAMED_NEW} more"
    return DriftFinding(
        backend=name,
        kind="models",
        status=INFO,
        detail=f"curated fallback is valid; CLI also offers {len(new)} newer id(s): {shown}{more}",
    )


def detect_drift(
    backends: Mapping[str, CodingAgentBackend] | None = None,
) -> DriftReport:
    """Probe every installed backend CLI and report where it has moved away from its adapter.

    Backends whose CLI is absent or not runnable are *skipped*, not failed: drift says nothing
    about a CLI you never installed, and failing on absence would make the command useless on any
    host that does not have all of them. ``marshal doctor`` is where a missing CLI is a finding.
    """
    registry = default_backends() if backends is None else backends
    findings: list[DriftFinding] = []
    checked: list[str] = []
    skipped: list[str] = []
    for name in sorted(registry):
        backend = registry[name]
        observed = backend.probe_version()
        if observed is None:
            skipped.append(name)
            continue
        checked.append(name)
        findings.append(_version_finding(name, backend, observed))
        findings.append(_models_finding(name, backend))
    fails = sum(1 for f in findings if f.status == FAIL)
    warns = sum(1 for f in findings if f.status == WARN)
    # Version warns leave ok true (nightlies). A models warn means the catalogue of an installed
    # CLI could not be verified - materially weaker than clean, so ok is false.
    models_unverified = any(f.kind == "models" and f.status == WARN for f in findings)
    return DriftReport(
        findings=findings,
        checked=checked,
        skipped=skipped,
        fails=fails,
        warns=warns,
        ok=fails == 0 and not models_unverified,
    )
