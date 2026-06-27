"""Bounded retries for transient backend failures.

A coding-agent run can fail for reasons that have nothing to do with the task: a backend's local
state DB is momentarily locked by a sibling run, a provider returns a rate-limit / overloaded / 5xx,
or a connection drops mid-handshake. Re-running usually succeeds. This module decides (a) whether a
failed result *looks* transient and (b) how long to wait before the next attempt.

It deliberately does NOT retry:
  * timeouts (`RunStatus.TIMED_OUT`) - a retry burns another full timeout window, and
  * genuine task failures (the agent ran and produced a wrong/erroring result) - a retry just
    spends money to fail again.
The marker list is intentionally conservative: a false positive wastes a whole run.
"""

from __future__ import annotations

from pydantic import BaseModel

from .types import AgentResult, RunStatus

# Lowercased substrings that mark an INFRASTRUCTURE / transport failure (not a task failure),
# matched against the failed result's error text.
TRANSIENT_MARKERS: tuple[str, ...] = (
    "database is locked",       # opencode / sqlite contention from a sibling run
    "rate limit",
    "rate_limit",
    "429",
    "too many requests",
    "overloaded",
    "502",
    "503",
    "504",
    "service unavailable",
    "temporarily unavailable",
    "connection reset",
    "connection refused",
    "econnreset",
    "try again",
)


def is_transient_failure(result: AgentResult) -> bool:
    """True if ``result`` is a FAILED run whose error looks like a transient infra/transport problem.

    Only ``RunStatus.FAILED`` qualifies: a ``TIMED_OUT`` retry would burn another full timeout
    window, and ``SUCCEEDED`` / ``EMPTY`` are not failures. An empty error string never matches - we
    do not retry a failure we cannot attribute to a transient cause.
    """
    if result.status is not RunStatus.FAILED:
        return False
    text = (result.error or "").lower()
    return any(marker in text for marker in TRANSIENT_MARKERS)


class RetryPolicy(BaseModel):
    """How many times to re-run a transiently-failed run, and how long to wait between attempts."""

    max_attempts: int = 1          # 1 = no retry; N = up to N-1 retries on a transient failure
    backoff_base_s: float = 1.0    # first wait; grows by backoff_factor each subsequent attempt
    backoff_factor: float = 2.0

    def delay_for(self, attempt: int) -> float:
        """Seconds to wait AFTER attempt number ``attempt`` (1-based) before the next one."""
        return self.backoff_base_s * (self.backoff_factor ** (attempt - 1))
