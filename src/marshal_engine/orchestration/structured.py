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
#: Any fenced block, anywhere in the message (the anchored `_JSON_FENCE_RE` only matches a message
#: that IS one fence). Used to lift a single fenced object out from behind narration.
_FENCED_BLOCK_RE = re.compile(r"```(?:[A-Za-z0-9_+-]*)\s*\n(.*?)\n?```", re.DOTALL)

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


def _top_level_json_objects(s: str) -> list[tuple[dict[str, Any], int]]:
    """Every top-level JSON object in ``s``, as ``(object, end_index)``, left to right.

    Scans for candidate ``{`` positions and keeps the ones that decode. A brace inside prose
    ("pass {} to reset") simply fails to decode and is skipped; a nested object is never counted
    separately because the scan resumes AFTER each successful decode, not inside it. Pure.
    """
    decoder = json.JSONDecoder()
    found: list[tuple[dict[str, Any], int]] = []
    i = 0
    while True:
        start = s.find("{", i)
        if start == -1:
            return found
        try:
            obj, end = decoder.raw_decode(s, start)
        except json.JSONDecodeError:
            i = start + 1
            continue
        if isinstance(obj, dict):
            found.append((obj, end))
            i = end
        else:  # pragma: no cover - raw_decode at a '{' yields a dict or raises
            i = start + 1


def _extract_json_object(text: str) -> dict[str, Any]:
    """Parse the final message as exactly one JSON object.

    Tolerates a single whole-message `` ```json `` fence, and LEADING prose before the object.
    Still rejects trailing prose after it, and still refuses when the message contains more than
    one top-level object.

    The leading and trailing cases are not symmetric, which is why only one of them was relaxed.
    Refusing trailing prose protects a real property - a reply with several objects makes "which
    one did it mean?" a guess, and this module never guesses. Leading prose does not threaten
    that property: with exactly one object present there is nothing to choose between. And for
    at least one backend the narration is not an occasional lapse but the normal shape of every
    final message, so the strict reading attributed the failure to the agent and stamped `failed`
    on runs that had done exactly what was asked, discarding a conforming object sitting in
    `text`.
    """
    s = text.strip()
    if not s:
        raise ValueError("empty final message")
    fenced = _JSON_FENCE_RE.match(s)
    if fenced:
        s = fenced.group(1).strip()
    else:
        # Narration, then a fenced block - the commonest LLM reply shape, and the two tolerances
        # did not compose: the anchored fence match failed, so the closing fence read as "trailing
        # prose" and a run that had produced exactly one conforming object was failed for it. Only
        # when there is exactly ONE fenced block, so "which one did you mean" stays an error.
        blocks = _FENCED_BLOCK_RE.findall(s)
        if len(blocks) == 1:
            s = blocks[0].strip()

    # Fast path: the message IS the object, as instructed.
    if s.startswith("{"):
        try:
            obj, end = json.JSONDecoder().raw_decode(s)
        except json.JSONDecodeError as exc:
            raise ValueError(f"final message is not JSON: {exc}") from exc
        if s[end:].strip():
            raise ValueError("trailing prose after JSON object")
        if not isinstance(obj, dict):
            raise ValueError(f"final message JSON must be an object, got {type(obj).__name__}")
        return obj

    candidates = _top_level_json_objects(s)
    if not candidates:
        raise ValueError(
            "final message is not JSON: Expecting value: line 1 column 1 (char 0)"
        )
    if len(candidates) > 1:
        raise ValueError(
            f"{len(candidates)} top-level JSON objects in the final message - "
            "cannot tell which one was meant"
        )
    obj, end = candidates[0]
    if s[end:].strip():
        raise ValueError("trailing prose after JSON object")
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
