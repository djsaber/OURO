"""Pure conversions of registered historical records; opaque JSON stays opaque."""

from __future__ import annotations

import json
import math
from collections.abc import Iterator
from typing import Any

from pydantic import BaseModel, ValidationError

from ouro.automation.file_records import StoredExecution, StoredTask
from ouro.core.agents import DelegationRecord
from ouro.core.base import VersionedOuroModel
from ouro.core.billing import ProviderUsageEntry, UsageLedgerEvent
from ouro.core.context import ContextMetrics
from ouro.core.context_snapshots import ContextSnapshotRecord
from ouro.core.events import EventRecord
from ouro.core.harness import HarnessVersion
from ouro.core.messages import MessageRecord
from ouro.core.resources import SessionResourceReference
from ouro.core.state import RunRecord, SessionRecord
from ouro_upgrade.errors import UpgradeDataError
from ouro_upgrade.steps import UpgradeStep, advance, migrate

MODELS: dict[str, type[BaseModel]] = {
    "session": SessionRecord,
    "run": RunRecord,
    "message": MessageRecord,
    "event": EventRecord,
    "context": ContextSnapshotRecord,
    "harness": HarnessVersion,
    "delegation": DelegationRecord,
    "resource": SessionResourceReference,
    "task": StoredTask,
    "execution": StoredExecution,
    "usage": ProviderUsageEntry,
    "usage_event": UsageLedgerEvent,
}

_LEGACY_BUDGET_FIELDS = (
    "context_window_tokens",
    "reserved_output_tokens",
    "input_budget_tokens",
    "estimated_remaining_tokens",
    "estimated_utilization_ratio",
    "over_budget",
)


def loads(text: str) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise UpgradeDataError("duplicate JSON key")
            result[key] = value
        return result

    try:
        value = json.loads(
            text,
            object_pairs_hook=pairs,
            parse_constant=_invalid_constant,
            parse_float=_finite_float,
        )
    except (ValueError, RecursionError) as exc:
        raise UpgradeDataError("invalid JSON object") from exc
    if not isinstance(value, dict):
        raise UpgradeDataError("expected JSON object")
    return value


def _invalid_constant(_: str) -> None:
    raise UpgradeDataError("non-finite JSON number")


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise UpgradeDataError("non-finite JSON number")
    return value


def dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n"


def validate[Model: BaseModel](model: type[Model], value: dict[str, Any]) -> Model:
    try:
        parsed = model.model_validate(value, extra="forbid")
    except ValidationError as exc:
        # Pydantic's normal message includes input values (possibly credentials).
        reason = exc.errors(include_input=False, include_context=False)[0]["type"]
        raise UpgradeDataError(f"invalid {model.__name__} ({reason})") from exc
    _required_facts(parsed, value)
    return parsed


def _required_facts(parsed: object, raw: object) -> None:
    """Do not let model defaults fabricate persisted identity, time or versions."""
    if isinstance(parsed, BaseModel) and isinstance(raw, dict):
        required = {
            "SessionRecord": ("history_epoch", "message_count", "harness_version", "status"),
            "MessageRecord": ("sequence", "before_harness_version"),
            "RunRecord": ("history_epoch", "status", "driver", "driver_checkpoint"),
            "TaskRecord": ("revision", "enabled", "status"),
            "TaskExecution": ("status",),
            "EventRecord": ("sequence",),
            "ProviderUsageEntry": ("revision", "input_tokens", "output_tokens"),
            "ToolCallBatchRecord": ("producer",),
            "ToolResultReference": ("producer",),
            "ToolInvocation": ("producer",),
            "ContextMetrics": (
                "estimated_input_tokens",
                "estimated_fixed_tokens",
                "budget",
                "estimator_id",
                "estimator_version",
            ),
        }.get(type(parsed).__name__, ())
        if any(name not in raw for name in required):
            raise UpgradeDataError(f"missing persisted {type(parsed).__name__} facts")
        if isinstance(parsed, VersionedOuroModel):
            expected = type(parsed).model_fields["schema_version"].default
            if type(raw.get("schema_version")) is not int or raw["schema_version"] != expected:
                raise UpgradeDataError(f"missing or unsupported {type(parsed).__name__} version")
        for name, field in type(parsed).model_fields.items():
            if (
                field.default_factory is not None
                and (name.endswith(("_id", "_at", "_version")))
                and name not in raw
            ):
                raise UpgradeDataError(f"missing persisted {type(parsed).__name__}.{name}")
            if name in raw:
                _required_facts(getattr(parsed, name), raw[name])
    elif isinstance(parsed, (list, tuple)) and isinstance(raw, list):
        for item, source in zip(parsed, raw, strict=True):
            _required_facts(item, source)
    # Open dictionaries are intentionally not traversed.


def _add(value: dict[str, Any], key: str, default: object) -> None:
    if key in value:
        raise UpgradeDataError(f"unexpected field in historical contract: {key}")
    value[key] = default


def _producer(value: dict[str, Any]) -> None:
    _add(value, "producer", {"kind": "model", "driver_id": None})


def _context_v3_to_v4(value: dict[str, Any]) -> None:
    if type(value["supports_vision"]) is not bool:
        raise UpgradeDataError("invalid historical context vision capability")
    _add(value, "supports_image_input", value.pop("supports_vision"))
    _add(value, "supports_audio_input", False)
    _add(value, "supports_video_input", False)


def _context_metrics(value: dict[str, Any]) -> None:
    """Preserve request facts and verify redundant historical budget indicators."""
    previous = {key: value.pop(key) for key in _LEGACY_BUDGET_FIELDS}
    window = previous["context_window_tokens"]
    reserve = previous["reserved_output_tokens"]
    if type(reserve) is not int or reserve < 0:
        raise UpgradeDataError("invalid historical output reserve")
    _add(
        value,
        "budget",
        {"context_window": window, "max_output_tokens": reserve or None}
        if window is not None
        else None,
    )
    metrics = validate(ContextMetrics, value)
    for key, original in previous.items():
        expected = getattr(metrics, key)
        matches = type(original) is type(expected) and (
            math.isclose(original, expected)
            if isinstance(expected, float)
            else original == expected
        )
        if not matches:
            raise UpgradeDataError(f"inconsistent historical context metric: {key}")


def _context_v4_to_v5(value: dict[str, Any]) -> None:
    _context_metrics(value["metrics"])


def _run_v2_to_v5(value: dict[str, Any]) -> None:
    _add(
        value,
        "driver",
        {
            "driver_id": "ouro.default",
            "contract_hash": "ouro.default/1",
            "implementation": None,
            "dependencies": [],
        },
    )
    _add(value, "driver_checkpoint", None)
    batch = value.get("active_tool_batch")
    if batch is not None:
        _producer(batch)
        for call in batch["calls"]:
            if call.get("invocation") is not None:
                _producer(call["invocation"])


def _message_v3_to_v4(value: dict[str, Any]) -> None:
    if value.get("tool_result") is not None:
        _producer(value["tool_result"])


def _task_v1_to_v3(value: dict[str, Any]) -> None:
    record = value["task"]
    _add(record, "owner_id", None)
    config = record["configuration"]
    target = {
        key: config.pop(key)
        for key in ("workspace", "approval_mode", "model", "reasoning", "session_mode")
    }
    target.update(kind="agent", prompt=config.pop("description"))
    _add(config, "target", target)


def _execution_v1_to_v3(value: dict[str, Any]) -> None:
    record = value["execution"]
    _add(record, "owner_id", None)
    _add(
        record,
        "target",
        {
            "kind": "agent",
            "session_id": record.pop("session_id"),
            "run_id": record.pop("run_id"),
        },
    )
    _add(record, "result", None)
    _add(record, "cancel_requested", False)


def _session_v2_to_v5(value: dict[str, Any]) -> None:
    _add(value, "context_selection", {"compaction_id": value.pop("active_compaction_id")})


def _run_v5_to_v10(value: dict[str, Any]) -> None:
    if value.get("active_tool_batch") is not None:
        raise UpgradeDataError("historical tool batch requires Session conversion")
    value.pop("usage")
    value.pop("usage_entry_ids")


def _message_v4_to_v8(value: dict[str, Any]) -> None:
    if value.get("tool_calls") or value.get("tool_result") is not None:
        raise UpgradeDataError("historical tool message requires Session conversion")


def _context_v5_to_v11(value: dict[str, Any]) -> None:
    fields = (
        "role",
        "content",
        "semantic_tag",
        "tool_calls",
        "tool_result",
        "provider_continuation",
    )
    messages = value.pop("injected_messages")
    for message in messages:
        if message.get("tool_result") is not None and "producer" not in message["tool_result"]:
            _producer(message["tool_result"])
    for message in value.pop("source_messages"):
        if message.get("schema_version") not in (3, 4):
            raise UpgradeDataError("unsupported historical context message version")
        if message.get("tool_result") is not None and "producer" not in message["tool_result"]:
            _producer(message["tool_result"])
        messages.append({key: message[key] for key in fields if key in message})
    _add(
        value,
        "provider_context",
        {
            "system_prompt": value.pop("system_prompt"),
            "messages": messages,
            "tools": value.pop("tool_definitions"),
        },
    )
    value["provider_done_at"] = value.pop("completed_at")
    value.pop("context_digest")
    value.pop("provider_reported_input_tokens")


def _resource_v2_to_v3(value: dict[str, Any]) -> None:
    _add(value, "internal", False)


def _usage_v1_to_v2(value: dict[str, Any]) -> None:
    _add(value, "plugin_id", None)


def _usage_event_v1_to_v2(value: dict[str, Any]) -> None:
    """The envelope is unchanged; its entry has an independent schema conversion."""


VERSION_STEPS: dict[str, dict[int, UpgradeStep]] = {
    "context": {
        3: UpgradeStep(4, _context_v3_to_v4),
        4: UpgradeStep(5, _context_v4_to_v5),
        5: UpgradeStep(11, _context_v5_to_v11),
    },
    "resource": {2: UpgradeStep(3, _resource_v2_to_v3)},
    "session": {2: UpgradeStep(5, _session_v2_to_v5)},
    "run": {2: UpgradeStep(5, _run_v2_to_v5), 5: UpgradeStep(10, _run_v5_to_v10)},
    "message": {3: UpgradeStep(4, _message_v3_to_v4), 4: UpgradeStep(8, _message_v4_to_v8)},
    "task": {1: UpgradeStep(3, _task_v1_to_v3)},
    "execution": {1: UpgradeStep(3, _execution_v1_to_v3)},
    "usage": {1: UpgradeStep(2, _usage_v1_to_v2)},
    "usage_event": {1: UpgradeStep(2, _usage_event_v1_to_v2)},
}


def convert(kind: str, raw: dict[str, Any]) -> dict[str, Any]:
    """Return current records unchanged; own and validate only a converted graph."""
    model = MODELS[kind]
    try:
        if not _needs_upgrade(kind, raw):
            return raw
        value = migrate(
            kind,
            raw,
            target=model.model_fields["schema_version"].default,
            steps=VERSION_STEPS.get(kind, {}),
        )
        _convert_nested(kind, value)
        validate(model, value)
    except (KeyError, TypeError, AttributeError) as exc:
        raise UpgradeDataError(f"incomplete historical {kind} structure") from exc
    return value


def _nested(kind: str, value: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    """Visit only schema-owned records, never dictionaries containing opaque data."""
    if kind == "usage_event" and value.get("entry") is not None:
        yield "usage", value["entry"]


def _legacy_event_metrics(kind: str, value: dict[str, Any]) -> Iterator[dict[str, Any]]:
    # Only these Host-owned payload fields carry unversioned context metrics.
    if kind != "event":
        return
    field = {
        "model_request_started": "context_metrics",
        "compaction_completed": "context_metrics_after",
    }.get(value.get("event_type", ""))
    if field is not None:
        metrics = value.get("payload", {}).get(field)
        if isinstance(metrics, dict) and any(key in metrics for key in _LEGACY_BUDGET_FIELDS):
            yield metrics


def _needs_upgrade(kind: str, value: dict[str, Any]) -> bool:
    version = value.get("schema_version")
    if type(version) is not int or version != MODELS[kind].model_fields["schema_version"].default:
        # The transition executor rejects unsupported versions before transforming.
        return True
    return any(_needs_upgrade(child, raw) for child, raw in _nested(kind, value)) or any(
        True for _ in _legacy_event_metrics(kind, value)
    )


def _convert_nested(kind: str, value: dict[str, Any]) -> None:
    for child, raw in _nested(kind, value):
        _convert_owned(child, raw)
    for metrics in _legacy_event_metrics(kind, value):
        _context_metrics(metrics)


def _convert_owned(kind: str, value: dict[str, Any]) -> None:
    # The outer conversion owns this subtree. Its final model validation checks
    # the complete record graph, including required persisted facts, exactly once.
    advance(
        kind,
        value,
        target=MODELS[kind].model_fields["schema_version"].default,
        steps=VERSION_STEPS.get(kind, {}),
    )
    _convert_nested(kind, value)


def convert_task(kind: str, raw: dict[str, Any], version: int) -> dict[str, Any]:
    """SQLite's container version selects the unversioned task payload contract."""
    value = convert(kind, {"schema_version": version, kind: raw})[kind]
    return value
