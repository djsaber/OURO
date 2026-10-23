"""Worker wire-value encoding, decoding and protocol validation."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ouro.core.message_semantics import validate_semantic_tag
from ouro.plugins.tool_content import decode_worker_content
from ouro.sdk.plugins import v5 as sdk


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
        output_fields = {"content", "structured", "resource_refs", "is_error"}
        if output_fields.intersection(result):
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
    _require_optional_type(result, "structured", dict, method)
    _require_string_list(result, "resource_refs", method)
    _require_optional_type(result, "is_error", bool, method)
    return sdk.ToolOutput(
        content=content,
        structured=_object_value(result.get("structured", {})),
        resource_refs=tuple(result.get("resource_refs", [])),
        is_error=result.get("is_error", False),
        session_control_transitions=_worker_control_transitions(result),
    )


def _decode_worker_observer(result: dict[str, Any], method: str) -> None:
    if result:
        raise WorkerProtocolError(f"worker {method} result must be empty")


def _decode_worker_context(result: dict[str, Any], method: str) -> sdk.ContextOutput:
    fragments = result.get("fragments", [])
    if not isinstance(fragments, list):
        raise WorkerProtocolError(f"worker {method} fragments must be a list")
    return sdk.ContextOutput(fragments=tuple(_worker_context_fragment(item) for item in fragments))


def _decode_worker_command(result: dict[str, Any], method: str) -> sdk.CommandOutput:
    _require_optional_type(result, "output", str, method)
    _require_optional_type(result, "data", dict, method)
    run_action = result.get("run_action")
    if run_action not in (None, "stop", "continue"):
        raise WorkerProtocolError(f"worker {method} run_action must be stop or continue")
    return sdk.CommandOutput(
        output=result.get("output", ""),
        data=_object_value(result.get("data", {})),
        run_action=run_action,
        session_inputs=_worker_session_inputs(result),
        session_input_batches=_worker_session_input_batches(result),
        session_control_transitions=_worker_control_transitions(result),
    )


def _decode_worker_session_controller(
    result: dict[str, Any], method: str
) -> sdk.SessionControllerOutput:
    _require_optional_type(result, "output", str, method)
    _require_optional_type(result, "data", dict, method)
    _require_optional_type(result, "usage_scope", str, method, allow_none=True)
    return sdk.SessionControllerOutput(
        output=result.get("output", ""),
        data=_object_value(result.get("data", {})),
        usage_scope=result.get("usage_scope"),
    )


def _decode_worker_compaction(result: dict[str, Any], method: str) -> sdk.CompactionOutput:
    _require_optional_type(result, "summary", str, method)
    usage = result.get("usage", {})
    if not isinstance(usage, dict) or any(
        isinstance(value, bool) or not isinstance(value, (int, float)) for value in usage.values()
    ):
        raise WorkerProtocolError(f"worker {method} usage must be numeric")
    return sdk.CompactionOutput(
        summary=result.get("summary", ""), usage={str(key): value for key, value in usage.items()}
    )


def _decode_worker_continuation(result: dict[str, Any], method: str) -> sdk.ContinuationDecision:
    action = result.get("action", "ignore")
    if action not in {"ignore", "continue", "complete", "pause", "fail"}:
        raise WorkerProtocolError(f"worker continuation action is invalid: {action}")
    mode = result.get("mode", "follow_up")
    if mode not in {"follow_up", "steer"}:
        raise WorkerProtocolError(f"worker continuation mode is invalid: {mode}")
    _require_optional_type(result, "prompt", str, method, allow_none=True)
    _require_optional_type(result, "title", str, method, allow_none=True)
    _require_optional_type(result, "role", str, method, allow_none=True)
    _require_optional_type(result, "semantic_tag", str, method, allow_none=True)
    _require_optional_type(result, "coalesce_key", str, method, allow_none=True)
    _require_optional_type(result, "reason", str, method, allow_none=True)
    return sdk.ContinuationDecision(
        action=action,
        prompt=result.get("prompt"),
        mode=mode,
        title=result.get("title"),
        role=_worker_session_input_role(result.get("role", "user")),
        scaffold=_worker_scaffold(result.get("scaffold", True)),
        semantic_tag=_worker_semantic_tag(result.get("semantic_tag")),
        coalesce_key=result.get("coalesce_key"),
        reason=result.get("reason"),
        session_control_transitions=_worker_control_transitions(result),
    )


def _decode_worker_tool_policy(result: dict[str, Any], method: str) -> sdk.ToolPolicyDecision:
    action = result.get("action", "ignore")
    if action not in {"ignore", "require_approval", "deny"}:
        raise WorkerProtocolError(f"worker tool policy action is invalid: {action}")
    _require_optional_type(result, "reason", str, method, allow_none=True)
    return sdk.ToolPolicyDecision(action=action, reason=result.get("reason"))


def _decode_worker_prompt_policy(result: dict[str, Any], method: str) -> sdk.PromptPolicyDecision:
    action = result.get("action", "allow")
    if action not in {"allow", "deny"}:
        raise WorkerProtocolError(f"worker prompt policy action is invalid: {action}")
    _require_optional_type(result, "reason", str, method, allow_none=True)
    return sdk.PromptPolicyDecision(action=action, reason=result.get("reason"))


def _decode_worker_turn_policy(result: dict[str, Any], method: str) -> sdk.TurnPolicyDecision:
    action = result.get("action", "allow")
    if action not in {"allow", "continue"}:
        raise WorkerProtocolError(f"worker turn policy action is invalid: {action}")
    _require_optional_type(result, "feedback", str, method, allow_none=True)
    return sdk.TurnPolicyDecision(action=action, feedback=result.get("feedback"))


def _require_optional_type(
    result: dict[str, Any],
    field: str,
    expected: type[object],
    method: str,
    *,
    allow_none: bool = False,
) -> None:
    if field not in result:
        return
    value = result[field]
    if allow_none and value is None:
        return
    if not isinstance(value, expected) or (expected is int and isinstance(value, bool)):
        raise WorkerProtocolError(f"worker {method} {field} must be {expected.__name__}")


def _require_string_list(result: dict[str, Any], field: str, method: str) -> None:
    value = result.get(field, [])
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise WorkerProtocolError(f"worker {method} {field} must be a string array")


def _object_value(raw: object) -> dict[str, object]:
    if not isinstance(raw, dict):
        return {}
    return {str(key): value for key, value in raw.items()}


def _worker_session_inputs(raw: dict[str, Any]) -> tuple[sdk.SessionInput, ...]:
    values = raw.get("session_inputs", [])
    if not isinstance(values, list):
        raise WorkerProtocolError("worker session_inputs result must be a list")
    inputs: list[sdk.SessionInput] = []
    for value in values:
        if not isinstance(value, dict):
            raise WorkerProtocolError("worker session input must be an object")
        prompt = value.get("prompt")
        mode = value.get("mode", "follow_up")
        title = value.get("title")
        role = value.get("role", "user")
        scaffold = _worker_scaffold(value.get("scaffold", True))
        semantic_tag = _worker_semantic_tag(value.get("semantic_tag"))
        driver_id = value.get("driver_id")
        if driver_id is not None and not isinstance(driver_id, str):
            raise WorkerProtocolError("worker session input Driver must be a string")
        if not isinstance(prompt, str) or not prompt.strip():
            raise WorkerProtocolError("worker session input prompt must not be empty")
        if mode not in {"follow_up", "steer"}:
            raise WorkerProtocolError(f"worker session input mode is invalid: {mode}")
        if title is not None and not isinstance(title, str):
            raise WorkerProtocolError("worker session input title must be a string")
        role = _worker_session_input_role(role)
        inputs.append(
            sdk.SessionInput(
                prompt=prompt,
                mode=mode,
                title=title,
                role=role,
                scaffold=scaffold,
                semantic_tag=semantic_tag,
                driver_id=driver_id,
            )
        )
    return tuple(inputs)


def _worker_session_input_batches(
    raw: dict[str, Any],
) -> tuple[sdk.SessionInputBatch, ...]:
    values = raw.get("session_input_batches", [])
    if not isinstance(values, list):
        raise WorkerProtocolError("worker session_input_batches result must be a list")
    batches: list[sdk.SessionInputBatch] = []
    for value in values:
        if not isinstance(value, dict):
            raise WorkerProtocolError("worker session input batch must be an object")
        coalesce_key = value.get("coalesce_key")
        if coalesce_key is not None and (
            not isinstance(coalesce_key, str) or not coalesce_key.strip()
        ):
            raise WorkerProtocolError("worker session input batch coalesce_key must not be empty")
        batches.append(
            sdk.SessionInputBatch(
                inputs=_worker_session_inputs({"session_inputs": value.get("inputs", [])}),
                coalesce_key=coalesce_key,
            )
        )
    return tuple(batches)


def _worker_session_input_role(value: object) -> sdk.SessionInputRole:
    if value not in {"system", "user", "assistant", "tool_result"}:
        raise WorkerProtocolError(f"worker session input role is invalid: {value}")
    return value


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


def _worker_control_transitions(
    raw: dict[str, Any],
) -> tuple[sdk.SessionControlTransition, ...]:
    values = raw.get("session_control_transitions", [])
    if not isinstance(values, list):
        raise WorkerProtocolError("worker session_control_transitions result must be a list")
    transitions: list[sdk.SessionControlTransition] = []
    for value in values:
        if not isinstance(value, dict):
            raise WorkerProtocolError("worker session control transition must be an object")
        try:
            transitions.append(
                sdk.SessionControlTransition(
                    action=value["action"],
                    controller_id=value.get("controller_id"),
                    activation_id=value.get("activation_id"),
                    replace_activation_ids=tuple(value.get("replace_activation_ids", [])),
                    expected_session_revision=value.get("expected_session_revision"),
                    expected_activation_revision=value.get("expected_activation_revision"),
                    payload=value.get("payload"),
                    reason=value.get("reason"),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise WorkerProtocolError(
                f"worker session control transition is invalid: {exc}"
            ) from exc
    return tuple(transitions)


def _worker_context_fragment(raw: object) -> str | sdk.ContextFragment:
    if isinstance(raw, str):
        return raw
    if not isinstance(raw, dict):
        raise WorkerProtocolError("worker context fragment must be a string or object")
    text = raw.get("text")
    persistence = raw.get("persistence", "ephemeral")
    target = raw.get("target", "message")
    role = _worker_session_input_role(raw.get("role", "user"))
    scaffold = _worker_scaffold(raw.get("scaffold", True))
    semantic_tag = _worker_semantic_tag(raw.get("semantic_tag"))
    if not isinstance(text, str) or not text:
        raise WorkerProtocolError("worker context fragment text must not be empty")
    if persistence not in {"ephemeral", "message"}:
        raise WorkerProtocolError(
            "worker context fragment persistence must be ephemeral or message"
        )
    if target not in {"message", "system_prompt"}:
        raise WorkerProtocolError("worker context fragment target is invalid")
    return sdk.ContextFragment(
        text=text,
        persistence=persistence,
        target=target,
        role=role,
        scaffold=scaffold,
        semantic_tag=semantic_tag,
    )
