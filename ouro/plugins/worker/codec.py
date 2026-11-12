"""Worker wire-value encoding, decoding and protocol validation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import fields
from typing import Any

from pydantic import TypeAdapter, ValidationError

from ouro.core.message_semantics import validate_semantic_tag
from ouro.plugins.tool_content import decode_worker_content
from ouro.sdk.plugins import v5 as sdk

_TOOL = TypeAdapter(sdk.ToolOutput)
_CONTEXT = TypeAdapter(sdk.ContextOutput)
_COMMAND = TypeAdapter(sdk.CommandOutput)
_CONTROLLER = TypeAdapter(sdk.SessionControllerOutput)
_COMPACTION = TypeAdapter(sdk.CompactionOutput)
_CONTINUATION = TypeAdapter(sdk.ContinuationDecision)
_TOOL_POLICY = TypeAdapter(sdk.ToolPolicyDecision)
_PROMPT_POLICY = TypeAdapter(sdk.PromptPolicyDecision)
_TURN_POLICY = TypeAdapter(sdk.TurnPolicyDecision)
_CONTROL_FIELDS = frozenset(field.name for field in fields(sdk.SessionControlTransition))


class WorkerProtocolError(RuntimeError):
    pass


def _worker_schema(raw: object, kind: str, identity: str, field: str) -> dict[str, object]:
    root = _object_result(raw, "contributions.prepare")
    values = root.get(kind, {})
    if not isinstance(values, dict):
        return {}
    descriptor = values.get(identity, {})
    if not isinstance(descriptor, dict):
        return {}
    return _object_value(descriptor.get(field, {}))


def _object_result(raw: object, method: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise WorkerProtocolError(f"worker {method} result must be an object")
    return raw


def _worker_observation_payload(observation: sdk.Observation) -> dict[str, object]:
    if isinstance(observation, sdk.RuntimeEventObservation):
        record = observation.record
        return {
            "event": observation.event,
            "session_id": observation.session_id,
            "run_id": observation.run_id,
            "record": {
                "event_id": record.event_id,
                "session_id": record.session_id,
                "run_id": record.run_id,
                "turn_id": record.turn_id,
                "event_type": record.event_type,
                "created_at": record.created_at.isoformat(),
                "sequence": record.sequence,
                "payload": _wire_json(record.payload),
                "metadata": _wire_json(record.metadata),
            },
        }
    if isinstance(observation, sdk.ContextPreparedObservation):
        return {
            "event": observation.event,
            "session_id": observation.session_id,
            "run_id": observation.run_id,
            "message_count": observation.message_count,
            "tool_count": observation.tool_count,
            "model_round": observation.model_round,
            "context_metadata": _wire_json(observation.context_metadata),
        }
    if isinstance(observation, sdk.ToolCallRequestedObservation):
        return {
            "event": observation.event,
            "session_id": observation.session_id,
            "run_id": observation.run_id,
            "tool_call_id": observation.tool_call_id,
            "tool_name": observation.tool_name,
            "canonical_tool_id": observation.canonical_tool_id,
            "owner_id": observation.owner_id,
            "effect": observation.effect,
            "batch_behavior": observation.batch_behavior,
            "arguments": _wire_json(observation.arguments),
        }
    if isinstance(observation, sdk.ToolCallFinishedObservation):
        return {
            "event": observation.event,
            "session_id": observation.session_id,
            "run_id": observation.run_id,
            "tool_call_id": observation.tool_call_id,
            "tool_name": observation.tool_name,
            "canonical_tool_id": observation.canonical_tool_id,
            "owner_id": observation.owner_id,
            "effect": observation.effect,
            "batch_behavior": observation.batch_behavior,
            "is_error": observation.is_error,
            "content": observation.content,
            "structured": _wire_json(observation.structured),
            "resource_refs": list(observation.resource_refs),
            "exit_code": observation.exit_code,
        }
    if isinstance(observation, sdk.RunFinishedObservation):
        outcome = observation.outcome
        return {
            "event": observation.event,
            "session_id": observation.session_id,
            "run_id": observation.run_id,
            "outcome": {
                "session_id": outcome.session_id,
                "run_id": outcome.run_id,
                "status": outcome.status,
                "origin": outcome.origin,
                "final_text": outcome.final_text,
                "error": outcome.error,
                "usage": dict(outcome.usage),
            },
        }
    raise WorkerProtocolError(f"unsupported worker observation: {type(observation).__name__}")


def _wire_json(value: object) -> object:
    if isinstance(value, dict | Mapping):
        return {str(key): _wire_json(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_wire_json(item) for item in value]
    return value


def _worker_input_resolution(resolution: sdk.ToolInputResolution) -> dict[str, object]:
    if isinstance(resolution, sdk.AnswerResolution):
        return {
            "kind": "answer",
            "value": resolution.value,
            "resolved_by": resolution.resolved_by,
            "resolver_id": resolution.resolver_id,
        }
    if isinstance(resolution, sdk.UnattendedResolution):
        return {
            "kind": "unattended",
            "resolved_by": "host_policy",
            "resolver_id": resolution.resolver_id,
            "reason": resolution.reason,
        }
    raise WorkerProtocolError(f"unsupported tool input resolution: {type(resolution).__name__}")


def _decode_worker_initialization(result: dict[str, Any], method: str) -> dict[str, Any]:
    if not isinstance(result.get("protocol_version"), str):
        raise WorkerProtocolError(f"worker {method} protocol_version must be a string")
    return result


def _decode_worker_contributions(result: dict[str, Any], method: str) -> dict[str, Any]:
    del method
    for kind, values in result.items():
        if isinstance(values, list):
            if any(not isinstance(item, str) for item in values):
                raise WorkerProtocolError(f"worker {kind} contribution ids must be strings")
            continue
        if not isinstance(values, dict) or any(
            not isinstance(identity, str) or not isinstance(descriptor, dict)
            for identity, descriptor in values.items()
        ):
            raise WorkerProtocolError(
                f"worker {kind} contributions must be string ids or descriptor objects"
            )
        if kind == "tools":
            for descriptor in values.values():
                for field in ("parameters_schema", "result_schema"):
                    value = descriptor.get(field, {})
                    if not isinstance(value, dict):
                        raise WorkerProtocolError(f"worker tool {field} must be an object")
    return result


def _decode_worker_tool(
    result: dict[str, Any], method: str
) -> sdk.ToolOutput | sdk.ToolInputRequest:
    if "input_request" in result:
        if {"content", "structured", "resource_refs", "is_error"}.intersection(result):
            raise WorkerProtocolError(
                f"worker {method} cannot return input_request together with tool output"
            )
        return sdk.ToolInputRequest(data=result["input_request"])
    try:
        content = decode_worker_content(result.get("content", ""))
    except ValueError:
        raise WorkerProtocolError(
            f"worker {method} content must be text or typed content parts"
        ) from None
    if not isinstance(result.get("is_error", False), bool):
        raise WorkerProtocolError(f"worker {method} is_error must be a boolean")
    if result.get("session_control_transitions"):
        result = {**result, "session_control_transitions": _worker_control_transitions(result)}
    return _decode(
        _TOOL,
        {
            **result,
            "content": content,
        },
        method,
    )


def _decode_worker_observer(result: dict[str, Any], method: str) -> None:
    if result:
        raise WorkerProtocolError(f"worker {method} result must be empty")


def _decode_worker_context(result: dict[str, Any], method: str) -> sdk.ContextOutput:
    output = _decode(_CONTEXT, result, method)
    for fragment in result.get("fragments", ()):
        if isinstance(fragment, dict):
            if not fragment["text"]:
                raise WorkerProtocolError("worker context fragment text must not be empty")
            if "scaffold" in fragment:
                _worker_scaffold(fragment["scaffold"])
            if "semantic_tag" in fragment:
                _worker_semantic_tag(fragment["semantic_tag"])
    return output


def _decode_worker_command(result: dict[str, Any], method: str) -> sdk.CommandOutput:
    if result.get("session_control_transitions"):
        result = {**result, "session_control_transitions": _worker_control_transitions(result)}
    output = _decode(_COMMAND, result, method)
    if output.session_inputs:
        _validate_session_inputs(result["session_inputs"])
    for batch in result.get("session_input_batches", ()):
        _validate_session_inputs(batch["inputs"])
    return output


def _decode_worker_session_controller(
    result: dict[str, Any], method: str
) -> sdk.SessionControllerOutput:
    return _decode(_CONTROLLER, result, method)


def _decode_worker_compaction(result: dict[str, Any], method: str) -> sdk.CompactionOutput:
    usage = result.get("usage", {})
    if not isinstance(usage, dict) or any(
        isinstance(value, bool) or not isinstance(value, (int, float)) for value in usage.values()
    ):
        raise WorkerProtocolError(f"worker {method} usage must be numeric")
    return _decode(_COMPACTION, result, method)


def _decode_worker_continuation(result: dict[str, Any], method: str) -> sdk.ContinuationDecision:
    if "scaffold" in result:
        _worker_scaffold(result["scaffold"])
    if result.get("session_control_transitions"):
        result = {**result, "session_control_transitions": _worker_control_transitions(result)}
    decision = _decode(_CONTINUATION, result, method)
    _worker_semantic_tag(decision.semantic_tag)
    return decision


def _decode_worker_tool_policy(result: dict[str, Any], method: str) -> sdk.ToolPolicyDecision:
    return _decode(_TOOL_POLICY, result, method)


def _decode_worker_prompt_policy(result: dict[str, Any], method: str) -> sdk.PromptPolicyDecision:
    return _decode(_PROMPT_POLICY, result, method)


def _decode_worker_turn_policy(result: dict[str, Any], method: str) -> sdk.TurnPolicyDecision:
    return _decode(_TURN_POLICY, result, method)


def _decode[T](adapter: TypeAdapter[T], raw: object, method: str) -> T:
    try:
        return adapter.validator.validate_python(raw)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(map(str, error['loc']))}: {error['msg']}"
            for error in exc.errors(include_input=False, include_url=False)
        )
        raise WorkerProtocolError(f"worker {method}: {details}") from None


def _object_value(raw: object) -> dict[str, object]:
    if not isinstance(raw, dict):
        return {}
    return {str(key): value for key, value in raw.items()}


def _worker_control_transitions(result: dict[str, Any]) -> tuple[sdk.SessionControlTransition, ...]:
    """Construct SDK intents without rewalking transport-owned JSON payloads.

    The shared SDK-to-core boundary owns control semantics and payload validation.
    """
    values = result["session_control_transitions"]
    if not isinstance(values, list):
        raise WorkerProtocolError("worker session_control_transitions must be a list")
    transitions = []
    for value in values:
        if not isinstance(value, dict):
            raise WorkerProtocolError("worker session control transition must be an object")
        fields = {key: item for key, item in value.items() if key in _CONTROL_FIELDS}
        try:
            if "replace_activation_ids" in fields:
                fields["replace_activation_ids"] = tuple(fields["replace_activation_ids"])
            transitions.append(sdk.SessionControlTransition(**fields))
        except (TypeError, ValueError) as exc:
            raise WorkerProtocolError(
                f"worker session control transition is invalid: {exc}"
            ) from exc
    return tuple(transitions)


def _validate_session_inputs(values: list[dict[str, Any]]) -> None:
    for item in values:
        if not item["prompt"].strip():
            raise WorkerProtocolError("worker session input prompt must not be empty")
        if "scaffold" in item:
            _worker_scaffold(item["scaffold"])
        if "semantic_tag" in item:
            _worker_semantic_tag(item["semantic_tag"])


def _worker_scaffold(value: object) -> bool:
    if not isinstance(value, bool):
        raise WorkerProtocolError("worker message scaffold must be a boolean")
    return value


def _worker_semantic_tag(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise WorkerProtocolError("worker message semantic_tag must not be empty")
    try:
        return validate_semantic_tag(value)
    except ValueError as exc:
        raise WorkerProtocolError(f"worker message semantic_tag is invalid: {exc}") from exc
