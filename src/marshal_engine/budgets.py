"""Budget tracking: soft-warn by default; optional hard refuse when ``enforce: true``.

Budgets are scoped by client, backend, or globally, over session/week/month windows.
Spend is read from the usage ledger; lookup failures degrade silently for advisory
budgets so a soft-warn never breaks a run or the usage display. Enforced budgets raise
``BudgetExceeded`` instead of spawning when the cap is already met.

``enforce: true`` also serializes matching in-flight spawns (see ``EnforceBudgetGate``):
without a per-run cost reservation, parallel admits against the same ledger snapshot can
overshoot the cap by up to concurrency × per-run cost. The gate admits at most one
in-flight matching spawn per enforce budget until that run finishes and records spend.
Reservations are durable under ``.marshal/budget_gate.json`` (flock + pid liveness) so a
CLI and MCP server on the same repo cannot both admit past a hard cap. Unbound placeholders
(empty ``run_id``) age out after a short TTL so a bind-time flock failure whose best-effort
release cannot re-acquire the lock cannot poison the slot forever; a live holder renews that
TTL while ``git worktree add`` is in progress, and ``bind`` refuses (or re-acquires only when
the key is free) if the slot was reclaimed meanwhile.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field

from .config import BudgetSpec
from .layout import budget_gate_path
from .usage import (
    Bucket,
    LedgerCursor,
    UnreadableUsageLedgerError,
    UsageEvent,
    UsageSummary,
    UsageTracker,
    _in_window,
)

#: Max wait for the cross-process reservation flock. Exceeding this refuses the spawn
#: (fail-closed): an enforced cap must not silently degrade to advisory.
_ENFORCE_GATE_LOCK_TIMEOUT_S = 5.0
_ENFORCE_GATE_LOCK_POLL_S = 0.01
#: Unbound (empty run_id) reservations older than this are reclaimed even when the holder
#: pid is still alive. Bounds how long a bind-failure + release-flock-failure poison entry
#: can block the cap without the holder's cooperation. Bound entries are never aged out.
#: A slow-but-alive holder renews ``reserved_at`` during ``git worktree add`` (see
#: ``EnforceBudgetGate.keep_alive``) so this TTL is a dead/stuck detector, not a worktree
#: deadline — 30s is long enough for a renew cadence of TTL/3 and short enough that a
#: wedged MCP/CLI cannot lock an enforce cap for the rest of its process lifetime.
_UNBOUND_RESERVATION_TTL_S = 30.0
#: How often ``keep_alive`` bumps ``reserved_at`` while the unbound phase is progressing.
_UNBOUND_RENEW_INTERVAL_S = _UNBOUND_RESERVATION_TTL_S / 3.0


class BudgetExceeded(RuntimeError):
    """Raised when an ``enforce: true`` budget's windowed spend already meets its cap,
    or when another in-flight run already holds that enforce budget (concurrency guard)."""


class BudgetRunScope(Protocol):
    """Minimal run shape for budget scope matching (avoids importing Fleet/RunRequest)."""

    client: str | None
    backend_name: str


@dataclass(frozen=True)
class BudgetCheckSnapshot:
    """Outside-lock spend baseline + ledger cursor for O(tail) under-lock revalidation."""

    cursor: LedgerCursor
    enforce_spent: dict[str, float] = field(default_factory=dict)


def _budget_window_since(window: str, session_start: datetime, now: datetime) -> datetime:
    """Map a budget's window name to the [since, now) start (UTC).

    `session` maps to the Fleet's `session_start` (the long-lived MCP server's wake instant), so
    a driver can ask "what have I spent since the server started?" without restating a timestamp.
    """
    if window == "session":
        return session_start
    if window == "week":
        return now - timedelta(days=7)
    if window == "month":
        return now - timedelta(days=30)
    raise ValueError(f"unknown budget window: {window!r} (use session|week|month)")


def _budget_bucket_from_summary(summary: UsageSummary, budget: BudgetSpec) -> Bucket | None:
    """The rollup bucket for a budget's scope, or None when the scope has no events."""
    if budget.client is not None:
        return summary.by_client.get(budget.client)
    if budget.backend is not None:
        return summary.by_backend.get(budget.backend)
    return summary.totals


def _budget_spent_known(bucket: Bucket | None) -> bool:
    """False when the scope has runs but none carried a priced cost source."""
    if bucket is None or bucket.runs == 0:
        return True
    return bucket.priced_runs > 0 or bucket.cost_usd > 0


def _budget_spend_from_summary(summary: UsageSummary, budget: BudgetSpec) -> float:
    """Cost recorded under a budget's scope within an already-computed summary (no ledger scan).

    A budget is scoped to its own `client`/`backend` (or the whole fleet when neither is set) - the
    spend is what has been recorded under THAT scope, not the scope of any one run. A client/backend
    with no recorded events (or a subscription backend reporting $0) reads 0.0; we never fabricate a
    percentage or "remaining" from a missing cost.
    """
    if budget.client is not None:
        bucket = summary.by_client.get(budget.client)
        return bucket.cost_usd if bucket is not None else 0.0
    if budget.backend is not None:
        bucket = summary.by_backend.get(budget.backend)
        return bucket.cost_usd if bucket is not None else 0.0
    return summary.totals.cost_usd


def _event_matches_budget_scope(event: UsageEvent, budget: BudgetSpec) -> bool:
    if budget.client is not None:
        return event.client == budget.client
    if budget.backend is not None:
        return event.backend == budget.backend
    return True


def _spend_from_events(
    events: list[UsageEvent],
    budget: BudgetSpec,
    *,
    session_start: datetime,
    now: datetime,
) -> float:
    """Windowed + scoped spend from an already-loaded event list (no I/O)."""
    since = _budget_window_since(budget.window, session_start, now)
    total = 0.0
    for e in events:
        if not _in_window(e, since, None):
            continue
        if not _event_matches_budget_scope(e, budget):
            continue
        total += e.cost_usd
    return total


def _budget_scope_label(budget: BudgetSpec) -> str:
    """Human-readable scope label for a budget (what the warning / display names)."""
    if budget.client is not None:
        return f"client:{budget.client}"
    if budget.backend is not None:
        return f"backend:{budget.backend}"
    return "global"


def _budget_matches(budget: BudgetSpec, req: BudgetRunScope) -> bool:
    if budget.client is not None:
        return req.client == budget.client
    if budget.backend is not None:
        return req.backend_name == budget.backend
    return True


def compute_budget_status(
    tracker: UsageTracker,
    session_start: datetime,
    budgets: list[BudgetSpec],
    now: datetime,
) -> list[BudgetStatus]:
    """Build a `BudgetStatus` per configured budget from the ledger at `now`.

    Lookup failures for an individual budget degrade to spent=0 with ``spent_known=False`` so
    the display never crashes the usage surface and machine consumers can tell spend is unknown.
    Always lenient (reporting path).
    """
    cache: dict[str, UsageSummary] = {}
    out: list[BudgetStatus] = []
    for b in budgets:
        try:
            summary = cache.get(b.window)
            if summary is None:
                summary = tracker.summary(
                    since=_budget_window_since(b.window, session_start, now),
                    strict=False,
                )
                cache[b.window] = summary
            spent = _budget_spend_from_summary(summary, b)
            bucket = _budget_bucket_from_summary(summary, b)
            spent_known = _budget_spent_known(bucket)
        except Exception:  # noqa: BLE001 - display never fails a usage query
            spent = 0.0
            spent_known = False  # error => spend unknown, never claim a known $0
        out.append(
            BudgetStatus(
                scope=_budget_scope_label(b),
                window=b.window,
                spent_usd=spent,
                limit_usd=b.limit_usd,
                remaining_usd=max(0.0, b.limit_usd - spent),
                enforce=b.enforce,
                spent_known=spent_known,
            )
        )
    return out


class BudgetStatus(BaseModel):
    """One configured budget's current standing - for `usage` displays + the MCP surface."""

    scope: str           # "client:<name>" | "backend:<name>" | "global"
    window: str          # session | week | month
    spent_usd: float     # windowed cost under this scope (0.0 for a scope with no spend)
    limit_usd: float
    remaining_usd: float # max(0, limit - spent) - the same floor a $0 spend gives a $0 remaining
    enforce: bool = False
    spent_known: bool = Field(
        default=True,
        description=(
            "False when spend could not be determined (lookup failure, or scope has runs "
            "but no priced cost source). Machine consumers (MCP / --json) must read this "
            "before treating spent_usd / remaining_usd as known."
        ),
    )


def check_budget(
    tracker: UsageTracker,
    session_start: datetime,
    budgets: list[BudgetSpec],
    req: BudgetRunScope,
    *,
    enforce_only: bool = False,
) -> BudgetCheckSnapshot:
    """Warn (advisory) or raise ``BudgetExceeded`` (enforce) for matching over-cap budgets.

    For every budget whose scope matches `req` (client match, backend match, or global), the
    windowed spend is recomputed from the usage ledger; if it meets or exceeds the cap:

    * ``enforce=false`` (default): soft-warn on stderr; never raise from this path's own
      lookup failures (a soft budget never breaks a run). Lenient ledger reads (torn lines
      skipped + warned).
    * ``enforce=true``: raise ``BudgetExceeded`` so the spawn is refused before a worktree is
      created. Lookup failures and any skipped/torn ledger lines also raise (fail closed) with
      an actionable repair message.

    ``enforce_only=True`` skips advisory budgets (no soft-warn). Returns a
    ``BudgetCheckSnapshot`` (ledger cursor + per-enforce-budget spend) so
    ``EnforceBudgetGate.begin`` can revalidate from the appended tail under its lock without a
    full O(ledger) rescan. The snapshot's events/cursor come from one
    ``read_events`` return pair (atomic handoff) — never from ``last_*`` attributes.

    A subscription / unknown-cost backend reports $0, so a $ budget on it never triggers (and
    shows $0 spent); we don't fabricate a percentage or "remaining" from that.
    """
    empty = BudgetCheckSnapshot(cursor=LedgerCursor(size=0, inode=0, mtime_ns=0))
    if not budgets:
        return empty
    now = datetime.now(timezone.utc)
    matching = [
        b
        for b in budgets
        if _budget_matches(b, req) and (not enforce_only or b.enforce)
    ]
    if not matching:
        return empty

    enforce_budgets = [b for b in matching if b.enforce]
    advisory_budgets = [b for b in matching if not b.enforce]

    cursor = LedgerCursor(size=0, inode=0, mtime_ns=0)
    enforce_spent: dict[str, float] = {}
    events: list[UsageEvent] | None = None

    if enforce_budgets:
        try:
            # Diagnostics may monkeypatch summary() to simulate ledger failure — probe that
            # first. The authoritative (events, cursor) pair comes ONLY from read_events'
            # return values (never post-hoc last_events / last_cursor attribute loads, which
            # a concurrent check_budget could interleave between).
            _probe_summary_if_patched(tracker, strict=True)
            events, cursor = tracker.read_events(strict=True)
            for b in enforce_budgets:
                spent = _spend_from_events(
                    events, b, session_start=session_start, now=now
                )
                enforce_spent[_enforce_budget_key(b)] = spent
                if spent < b.limit_usd:
                    continue
                raise BudgetExceeded(
                    f"[marshal] budget: {_budget_scope_label(b)} spent "
                    f"${spent:.4f} >= cap ${b.limit_usd:.4f} ({b.window}); "
                    "refusing new spawn (enforce=true). "
                    "Raise limit_usd, wait for the window to roll, or set enforce: false for soft-warn."
                )
        except BudgetExceeded:
            raise
        except UnreadableUsageLedgerError as exc:
            raise BudgetExceeded(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - OSError / decode / unexpected
            raise BudgetExceeded(
                f"budget {_budget_scope_label(enforce_budgets[0])} "
                f"({enforce_budgets[0].window}): spend lookup failed; "
                f"refusing spawn because enforce=true ({exc})"
            ) from exc

    if advisory_budgets and not enforce_only:
        if events is None:
            try:
                # One lenient read for advisory-only checks; reuse enforce's pair when present.
                events, cursor = tracker.read_events(strict=False)
            except Exception:  # noqa: BLE001 - soft budget never breaks a run
                events = []
        for b in advisory_budgets:
            try:
                spent = _spend_from_events(
                    events, b, session_start=session_start, now=now
                )
            except Exception:  # noqa: BLE001
                continue
            if spent < b.limit_usd:
                continue
            print(
                f"[marshal] budget: {_budget_scope_label(b)} spent "
                f"${spent:.4f} >= cap ${b.limit_usd:.4f} ({b.window})",
                file=sys.stderr,
            )

    return BudgetCheckSnapshot(cursor=cursor, enforce_spent=enforce_spent)


def _probe_summary_if_patched(tracker: UsageTracker, *, strict: bool) -> None:
    """Call ``summary`` only when it has been monkeypatched (fleet diagnostics / tests).

    Production uses the real ``UsageTracker.summary``; we skip it and go straight to the
    atomic ``read_events`` handoff. A replaced ``summary`` is still probed so
    ``spend lookup failed`` fail-closed behavior is preserved.
    """
    current = tracker.summary
    real = UsageTracker.summary
    if getattr(current, "__func__", None) is real:
        return
    current(strict=strict)


def _enforce_budget_key(budget: BudgetSpec) -> str:
    """Stable key for an enforce-budget concurrency slot (scope + window + limit)."""
    return f"{_budget_scope_label(budget)}|{budget.window}|{budget.limit_usd}"


def _recheck_enforce_from_tail(
    tracker: UsageTracker,
    snap: BudgetCheckSnapshot,
    session_start: datetime,
    budgets: list[BudgetSpec],
    req: BudgetRunScope,
) -> None:
    """Under-lock revalidation: apply only appended ledger bytes to the outside spend baseline.

    Lock-body work is O(new events), normally O(1). A rewritten/truncated/torn tail fails closed.
    """
    if not snap.enforce_spent:
        return
    try:
        new_events = tracker.events_after(snap.cursor, strict=True)
    except UnreadableUsageLedgerError as exc:
        raise BudgetExceeded(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise BudgetExceeded(
            f"usage ledger unreadable ({tracker.events_path}): {exc}; "
            "enforced budgets cannot verify spend - repair or remove the torn line"
        ) from exc

    now = datetime.now(timezone.utc)
    spent = dict(snap.enforce_spent)
    matching = [b for b in budgets if b.enforce and _budget_matches(b, req)]
    for e in new_events:
        for b in matching:
            since = _budget_window_since(b.window, session_start, now)
            if not _in_window(e, since, None):
                continue
            if not _event_matches_budget_scope(e, b):
                continue
            key = _enforce_budget_key(b)
            spent[key] = spent.get(key, 0.0) + e.cost_usd

    for b in matching:
        key = _enforce_budget_key(b)
        cur = spent.get(key, 0.0)
        if cur < b.limit_usd:
            continue
        raise BudgetExceeded(
            f"[marshal] budget: {_budget_scope_label(b)} spent "
            f"${cur:.4f} >= cap ${b.limit_usd:.4f} ({b.window}); "
            "refusing new spawn (enforce=true). "
            "Raise limit_usd, wait for the window to roll, or set enforce: false for soft-warn."
        )


def _pid_alive(pid: int) -> bool:
    """True when ``pid`` still names a live process (signal 0 probe)."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        # Permission denied or other ambiguity: assume alive so we never reclaim a live holder.
        return True


def _pid_start_time(pid: int) -> str | None:
    """OS-reported start time of ``pid``, or None when unverifiable (same idiom as fleet.lock)."""
    try:
        proc = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    started = proc.stdout.strip()
    return started or None


def _reservation_holder_live(entry: dict[str, Any]) -> bool:
    """Whether a disk reservation still has a live owner.

    Staleness rule (mirrors ``fleet.lock`` / ``_pid_is_still_ours``): reclaim when the holder
    pid is dead, or when a live pid's OS start time does not match the recorded
    ``pid_start_time`` (pid reuse). Missing/unprobeable start time while the pid is alive
    fails closed (assume held) so a hard cap never admits past a possibly-live holder.
    """
    try:
        pid = int(entry["pid"])
    except (KeyError, TypeError, ValueError):
        return False  # corrupt entry → reclaim
    if not _pid_alive(pid):
        return False
    recorded = entry.get("pid_start_time")
    if not isinstance(recorded, str) or not recorded:
        return True  # older/missing start time: assume held while pid alive
    now = _pid_start_time(pid)
    if now is None:
        return True  # probe unavailable: assume held
    return now == recorded


def _unbound_reservation_expired(entry: dict[str, Any]) -> bool:
    """True when a placeholder (empty run_id) has outlived ``_UNBOUND_RESERVATION_TTL_S``.

    Missing/invalid ``reserved_at`` counts as expired so a pre-TTL poison entry cannot stick.
    Bound entries are never expired here (caller must check run_id first).
    """
    raw = entry.get("reserved_at")
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        return True
    try:
        reserved_at = float(raw)
    except (TypeError, ValueError):
        return True
    return (time.time() - reserved_at) >= _UNBOUND_RESERVATION_TTL_S


def _reservation_still_held(entry: dict[str, Any]) -> bool:
    """Whether a disk reservation should still block a matching enforce admit.

    Composes pid/``pid_start_time`` liveness with unbound TTL: a live holder keeps a *bound*
    slot, but an unbound placeholder is dropped once it ages out so a failed bind+release
    cleanup cannot lock the cap for the rest of the process lifetime.
    """
    if not _reservation_holder_live(entry):
        return False
    run_id = entry.get("run_id") or ""
    if run_id:
        return True
    return not _unbound_reservation_expired(entry)


def _entry_owned_by(entry: dict[str, Any], token: str) -> bool:
    """True when ``entry`` carries the reservation token issued by our ``begin``."""
    return isinstance(token, str) and bool(token) and entry.get("token") == token


def _lock_sidecar(path: Path) -> Path:
    """``budget_gate.json.lock`` — flock target; auto-releases on process death."""
    return path.with_name(path.name + ".lock")


@contextlib.contextmanager
def _flock_exclusive(lock_path: Path, *, timeout_s: float) -> Iterator[None]:
    """Exclusive flock with a deadline; fail-closed via ``BudgetExceeded`` on trouble/timeout."""
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        guard = open(lock_path, "a+", encoding="utf-8")  # noqa: SIM115 - closed in finally
    except OSError as exc:
        raise BudgetExceeded(
            f"budget gate lock unavailable ({lock_path}): {exc}; "
            "refusing spawn because enforce=true"
        ) from exc
    try:
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                fcntl.flock(guard.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise BudgetExceeded(
                        f"budget gate lock timed out after {timeout_s:.1f}s ({lock_path}); "
                        "refusing spawn because enforce=true. Retry shortly, or check for a "
                        "stuck marshal process holding the enforce-budget reservation lock."
                    )
                time.sleep(_ENFORCE_GATE_LOCK_POLL_S)
            except OSError as exc:
                raise BudgetExceeded(
                    f"budget gate lock failed ({lock_path}): {exc}; "
                    "refusing spawn because enforce=true"
                ) from exc
        try:
            yield
        finally:
            fcntl.flock(guard.fileno(), fcntl.LOCK_UN)
    finally:
        guard.close()


def _parse_held_entry(key: str, value: Any) -> dict[str, Any]:
    """Validate one ``held`` entry. Present-but-malformed is UNKNOWN (caller refuses)."""
    if not isinstance(value, dict):
        raise ValueError(f"held[{key!r}] must be a JSON object")
    if "pid" not in value:
        raise ValueError(f"held[{key!r}] missing pid")
    try:
        int(value["pid"])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"held[{key!r}] pid must be an int") from exc
    token = value.get("token")
    if not isinstance(token, str) or not token:
        raise ValueError(f"held[{key!r}] missing token")
    return value


def _load_reservations(path: Path) -> dict[str, dict[str, Any]]:
    """Load durable reservation slots. Absent file → empty (nobody holds a slot).

    A present but unreadable/corrupt file, or a ``held`` entry with an invalid shape, is
    UNKNOWN state: raise ``BudgetExceeded`` so ``enforce: true`` refuses rather than
    admitting past a hard cap. Soft-warn / reporting never call this (lock-free + lenient).
    """
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        held = data.get("held", {})
        if not isinstance(held, dict):
            raise ValueError("held must be a JSON object")
        return {str(k): _parse_held_entry(str(k), v) for k, v in held.items()}
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise BudgetExceeded(
            f"budget gate reservation file unreadable ({path}): {exc}; "
            "refusing spawn because enforce=true. Delete the file when no runs are in flight "
            "to repair."
        ) from exc


def _write_reservations(path: Path, held: dict[str, dict[str, Any]]) -> None:
    """Atomic replace (unique temp + ``os.replace``), same idiom as ``FleetState._write``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f"{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"held": held}, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _new_reservation_entry(run_id: str = "", *, token: str | None = None) -> dict[str, Any]:
    pid = os.getpid()
    return {
        "run_id": run_id,
        "pid": pid,
        "pid_start_time": _pid_start_time(pid),
        "reserved_at": time.time(),
        "token": token if token is not None else secrets.token_hex(16),
    }


class EnforceBudgetGate:
    """Admit at most one in-flight spawn per matching ``enforce: true`` budget.

    Ledger checks alone are TOCTOU under ``run_many`` / concurrent ``spawn``: every thread can
    read the same pre-run spend and pass before any usage is recorded. Holding a per-budget
    slot until the run finishes closes that race without inventing a per-run cost estimate.

    In-process callers serialize on a ``threading.Lock``; cross-process callers (CLI + MCP on
    one repo) serialize on an ``fcntl.flock`` over ``.marshal/budget_gate.json`` (see
    ``layout.budget_gate_path``). Advisory budgets are unaffected and never take either lock.
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self._lock = threading.Lock()
        # key -> run_id once bound; empty string while reserved between begin() and bind()
        self._held: dict[str, str] = {}
        # key -> opaque token written to disk at begin; bind/renew prove ownership with it
        self._tokens: dict[str, str] = {}
        # Durable reservation file; None until begin() infers it from the usage tracker.
        self._path: Path | None = Path(path) if path is not None else None

    def _resolve_path(self, tracker: UsageTracker) -> Path:
        if self._path is not None:
            return self._path
        # usage lives at <marshal>/usage; reservation sits next to it (layout.budget_gate_path).
        inferred = tracker.dir.parent / budget_gate_path(Path("_")).name
        self._path = inferred
        return inferred

    def begin(
        self,
        tracker: UsageTracker,
        session_start: datetime,
        budgets: list[BudgetSpec],
        req: BudgetRunScope,
    ) -> list[str]:
        """Check ledger caps, then reserve concurrency slots for matching enforce budgets.

        Full ledger spend is computed *before* acquiring locks so fleet-wide spawns do not
        serialize behind O(ledger) scans. Under the thread lock + cross-process flock we
        reclaim stale disk reservations (dead/reused pid, or unbound past TTL), revalidate from
        only the appended tail (O(new events), normally O(1)), then reserve slots in memory and
        on disk. If reservation fails partway through a multi-budget match, every key reserved
        so far is released before re-raising.

        Soft-warn (non-enforce) budgets return immediately with no locks.
        """
        # Optimistic check outside the lock (advisory soft-warn + enforce refuse + cursor).
        snap = check_budget(tracker, session_start, budgets, req)
        matching = [b for b in budgets if b.enforce and _budget_matches(b, req)]
        if not matching:
            return []  # advisory-only: lock-free

        path = self._resolve_path(tracker)
        with self._lock:
            with _flock_exclusive(
                _lock_sidecar(path), timeout_s=_ENFORCE_GATE_LOCK_TIMEOUT_S
            ):
                # Tail-only enforce re-check — never a full summary()/events() rescan under the lock.
                _recheck_enforce_from_tail(tracker, snap, session_start, budgets, req)
                disk = _load_reservations(path)
                # Drop dead/reused-pid holders and aged-out unbound placeholders.
                disk = {k: e for k, e in disk.items() if _reservation_still_held(e)}
                keys: list[str] = []
                try:
                    for b in matching:
                        key = _enforce_budget_key(b)
                        holder = self._held.get(key)
                        if holder is None and key in disk:
                            entry = disk[key]
                            holder = str(entry.get("run_id") or "") or "starting"
                        if holder is not None:
                            held_by = holder or "starting"
                            raise BudgetExceeded(
                                f"budget {_budget_scope_label(b)} ({b.window}): another in-flight run "
                                f"holds this enforce cap (run {held_by}); refusing concurrent spawn to "
                                "prevent overshoot. Wait for it to finish, or set enforce: false."
                            )
                        token = secrets.token_hex(16)
                        entry = _new_reservation_entry("", token=token)
                        self._held[key] = ""
                        self._tokens[key] = token
                        disk[key] = entry
                        keys.append(key)
                    try:
                        _write_reservations(path, disk)
                    except OSError as exc:
                        raise BudgetExceeded(
                            f"budget gate reservation write failed ({path}): {exc}; "
                            "refusing spawn because enforce=true"
                        ) from exc
                    return keys
                except Exception:
                    for key in keys:
                        self._held.pop(key, None)
                        self._tokens.pop(key, None)
                        disk.pop(key, None)
                    if keys:
                        with contextlib.suppress(OSError):
                            _write_reservations(path, disk)
                    raise

    def renew(self, keys: list[str]) -> None:
        """Bump ``reserved_at`` on unbound slots we still own (slow worktree keep-alive).

        Raises ``BudgetExceeded`` when a key was reclaimed or is held by another process —
        the unbound phase must stop rather than proceed past a lost hard-cap slot.
        """
        if not keys:
            return
        with self._lock:
            path = self._path
            if path is None:
                return
            try:
                with _flock_exclusive(
                    _lock_sidecar(path), timeout_s=_ENFORCE_GATE_LOCK_TIMEOUT_S
                ):
                    disk = _load_reservations(path)
                    for key in keys:
                        token = self._tokens.get(key)
                        if token is None or key not in self._held:
                            continue
                        entry = disk.get(key)
                        if entry is None or not _entry_owned_by(entry, token):
                            self._held.pop(key, None)
                            self._tokens.pop(key, None)
                            raise BudgetExceeded(
                                f"budget gate reservation was reclaimed before bind "
                                f"({path}, key {key}); refusing spawn because enforce=true. "
                                "Retry shortly."
                            )
                        if not (entry.get("run_id") or ""):
                            entry["reserved_at"] = time.time()
                            disk[key] = entry
                    _write_reservations(path, disk)
            except BudgetExceeded:
                raise
            except OSError as exc:
                raise BudgetExceeded(
                    f"budget gate reservation renew failed ({path}): {exc}; "
                    "refusing spawn because enforce=true"
                ) from exc

    @contextlib.contextmanager
    def keep_alive(self, keys: list[str]) -> Iterator[None]:
        """Renew unbound reservations on a TTL/3 cadence while ``git worktree add`` runs.

        A dead/stuck holder that never renews still ages out via ``_UNBOUND_RESERVATION_TTL_S``
        without that owner's cooperation. Ownership loss during the wait fails the spawn.
        """
        if not keys:
            yield
            return
        stop = threading.Event()
        lost: list[BaseException] = []

        def _loop() -> None:
            while not stop.wait(_UNBOUND_RENEW_INTERVAL_S):
                try:
                    self.renew(keys)
                except BudgetExceeded as exc:
                    lost.append(exc)
                    return
                except Exception as exc:  # noqa: BLE001 - surface after the wait
                    lost.append(exc)
                    return

        thread = threading.Thread(
            target=_loop, name="marshal-budget-gate-keepalive", daemon=True
        )
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=_UNBOUND_RENEW_INTERVAL_S + 1.0)
        if lost:
            raise BudgetExceeded(str(lost[0])) from lost[0]

    def bind(self, keys: list[str], run_id: str) -> None:
        """Attach reserved slots to ``run_id`` after worktree creation.

        Verifies each key still carries our begin-issued token. If another holder owns it,
        refuse (never steal). If the key is absent, re-acquire under the cap — the slot is
        free. Blindly overwriting a peer's entry would admit two runs under a cap of one.
        """
        if not keys:
            return
        with self._lock:
            path = self._path
            if path is None:
                for key in keys:
                    if key in self._held:
                        self._held[key] = run_id
                return
            try:
                with _flock_exclusive(
                    _lock_sidecar(path), timeout_s=_ENFORCE_GATE_LOCK_TIMEOUT_S
                ):
                    disk = _load_reservations(path)
                    for key in keys:
                        token = self._tokens.get(key)
                        if token is None or key not in self._held:
                            continue
                        entry = disk.get(key)
                        if entry is not None and not _entry_owned_by(entry, token):
                            for k in keys:
                                self._held.pop(k, None)
                                self._tokens.pop(k, None)
                            held_by = str(entry.get("run_id") or "") or "starting"
                            raise BudgetExceeded(
                                f"budget gate reservation was reclaimed before bind "
                                f"({path}, key {key}; held by run {held_by}); "
                                "refusing spawn because enforce=true. Retry shortly."
                            )
                        if entry is None:
                            # Reclaimed and not taken — slot is free; re-acquire under the cap.
                            disk[key] = _new_reservation_entry(run_id, token=token)
                        else:
                            entry["run_id"] = run_id
                            disk[key] = entry
                        self._held[key] = run_id
                    _write_reservations(path, disk)
            except BudgetExceeded:
                raise
            except OSError as exc:
                raise BudgetExceeded(
                    f"budget gate reservation bind failed ({path}): {exc}; "
                    "refusing spawn because enforce=true"
                ) from exc

    def release(self, keys: list[str]) -> None:
        """Drop slots reserved by ``begin`` when ``_start`` fails before bind."""
        if not keys:
            return
        with self._lock:
            for key in keys:
                self._held.pop(key, None)
                self._tokens.pop(key, None)
            self._disk_drop(keys=keys)

    def release_run(self, run_id: str) -> None:
        """Release every slot held by ``run_id`` (terminal path / spawn submit failure)."""
        with self._lock:
            keys = [key for key, held in self._held.items() if held == run_id]
            for key in keys:
                del self._held[key]
                self._tokens.pop(key, None)
            self._disk_drop(keys=keys, run_id=run_id)

    def _disk_drop(self, *, keys: list[str] | None = None, run_id: str | None = None) -> None:
        """Best-effort clear of durable reservations; in-memory slots are already gone.

        Flock failure leaves disk entries for later reclaim (dead holder pid, or unbound TTL).
        """
        path = self._path
        if path is None:
            return
        try:
            with _flock_exclusive(
                _lock_sidecar(path), timeout_s=_ENFORCE_GATE_LOCK_TIMEOUT_S
            ):
                disk = _load_reservations(path)
                if keys is not None:
                    for key in keys:
                        disk.pop(key, None)
                if run_id is not None:
                    for key, entry in list(disk.items()):
                        if entry.get("run_id") == run_id:
                            disk.pop(key, None)
                _write_reservations(path, disk)
        except BudgetExceeded:
            return
        except OSError:
            return
