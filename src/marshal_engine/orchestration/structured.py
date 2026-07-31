"""Structured output: ask for one JSON object, then parse and validate what comes back.

An agent's final message is free text, so `output_schema` appends a backend-agnostic instruction
to the goal and this module extracts, validates, and redacts the result. A malformed or
schema-violating reply is reported as an error on the run - never silently dropped.
"""

from __future__ import annotations

import json
import re
from typing import Any

import jsonschema

from ..core.types import AgentResult, RunStatus, TaskSpec
from ..runtime.env import redact_secrets

#: Whole-message fence: optional language tag, body, closing fence. Trailing prose outside the
#: fence is rejected (the pattern must match the entire stripped message).
_JSON_FENCE_RE = re.compile(
    r"^```(?:json)?\s*\n(.*)\n```\s*$",
    re.DOTALL | re.IGNORECASE,
)

#: Prompt suffix when TaskSpec.output_schema is set. Backend-agnostic: appended to the goal so
#: ``CodingAgentBackend._compose_prompt`` picks it up without any adapter changes.
_STRUCTURED_OUTPUT_MARKER = (
    "Your FINAL MESSAGE must be exactly one JSON object conforming to this JSON Schema"
)
_STRUCTURED_OUTPUT_INSTRUCTION = (
    f"\n\n{_STRUCTURED_OUTPUT_MARKER}, "
    "with no surrounding prose or markdown fences:\n{schema}"
)


def _task_with_schema_instruction(task: TaskSpec) -> TaskSpec:
    """Return a copy of ``task`` whose goal carries the structured-output instruction, if any.

    Injection lives here (not on the backend base) so the backend contract stays untouched: every
    adapter already builds its prompt from ``task.goal`` via ``_compose_prompt``.

    ``output_schema is None`` means unstructured (no injection). An empty dict ``{}`` is a valid
    JSON Schema and *does* inject — see ``_apply_structured_output``. Idempotent: if the marker is
    already present in the goal, the goal is returned unchanged (defense against double injection).
    """
    if task.output_schema is None:
        return task
    if _STRUCTURED_OUTPUT_MARKER in task.goal:
        return task
    suffix = _STRUCTURED_OUTPUT_INSTRUCTION.format(schema=json.dumps(task.output_schema))
    return task.model_copy(update={"goal": task.goal + suffix})


def _extract_json_object(text: str) -> dict[str, Any]:
    """Parse the final message as exactly one JSON object.

    Tolerates a single whole-message `` ```json `` fence; rejects trailing prose after the object
    (and rejects fences that do not wrap the entire message).
    """
    s = text.strip()
    if not s:
        raise ValueError("empty final message")
    fenced = _JSON_FENCE_RE.match(s)
    if fenced:
        s = fenced.group(1).strip()
    try:
        obj, end = json.JSONDecoder().raw_decode(s)
    except json.JSONDecodeError as exc:
        raise ValueError(f"final message is not JSON: {exc}") from exc
    if s[end:].strip():
        raise ValueError("trailing prose after JSON object")
    if not isinstance(obj, dict):
        raise ValueError(f"final message JSON must be an object, got {type(obj).__name__}")
    return obj


def _redact_structured(obj: dict[str, Any] | None) -> dict[str, Any] | None:
    """Value-scrub string leaves of a structured payload (same markers as run-record text)."""
    if obj is None:
        return None

    def _walk(value: Any) -> Any:
        if isinstance(value, str):
            return redact_secrets(value)
        if isinstance(value, dict):
            return {k: _walk(v) for k, v in value.items()}
        if isinstance(value, list):
            return [_walk(v) for v in value]
        return value

    walked = _walk(obj)
    return walked if isinstance(walked, dict) else obj


def _apply_structured_output(task: TaskSpec, result: AgentResult) -> AgentResult:
    """Validate the final message against ``task.output_schema`` when one was requested.

    ``{}`` semantic: an empty schema is a valid JSON Schema (matches any JSON value), but Marshal's
    extraction contract still requires a top-level JSON *object*. So ``output_schema={}`` means
    "any JSON object" — equivalent in practice to asking for a parseable object with no further
    shape constraints. Prose / arrays / scalars still fail. Use ``is None`` (not truthiness) so
    ``{}`` never silently no-ops.

    Status semantics (RunStatus vocabulary unchanged):
      * ``output_schema is None`` → identity (``structured`` stays None).
      * schema + clean exit + valid object → ``structured`` populated; status unchanged.
      * schema + clean exit + invalid/absent → ``FAILED`` with ``error`` prefixed
        ``structured_output:`` and ``structured=None``. Not a silent prose success.
      * schema + non-clean exit → left alone (the run's own failure stands; do not overwrite it).

    Applied AFTER the retry loop: a schema-invalid reply is a contract failure, never a transient
    infra failure, so it must not trigger another attempt. Validation catches broadly (including
    dangling ``$ref`` / referencing errors) so no schema failure escapes ``_execute`` as a crash.
    """
    if task.output_schema is None:
        return result
    if result.status is not RunStatus.EXITED_CLEAN:
        return result
    try:
        obj = _extract_json_object(result.text)
        jsonschema.validate(instance=obj, schema=task.output_schema)
    except Exception as exc:  # noqa: BLE001 - schema/ref/parse failures must not crash the run
        detail = (
            exc.message
            if isinstance(exc, jsonschema.ValidationError) and getattr(exc, "message", None)
            else str(exc)
        )
        return result.model_copy(
            update={
                "status": RunStatus.FAILED,
                "structured": None,
                "error": f"structured_output: {detail}",
            }
        )
    return result.model_copy(update={"structured": obj})
