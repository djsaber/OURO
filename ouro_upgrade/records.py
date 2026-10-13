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
from ouro.core.compaction import CompactionRecord
from ouro.core.context_snapshots import ContextSnapshotRecord
from ouro.core.events import EventRecord
from ouro.core.harness import HarnessVersion
from ouro.core.messages import MessageRecord
from ouro.core.resources import SessionResourceReference
from ouro.core.session_commits import SessionCommitResult
from ouro.core.state import RunRecord, SessionRecord
from ouro_upgrade.errors import UpgradeDataError
from ouro_upgrade.steps import UpgradeStep, advance, migrate

MODELS: dict[str, type[BaseModel]] = {
    "session": SessionRecord,
    "run": RunRecord,
    "message": MessageRecord,
    "event": EventRecord,
    "context": ContextSnapshotRecord,
    "compaction": CompactionRecord,
    "harness": HarnessVersion,
    "delegation": DelegationRecord,
    "resource": SessionResourceReference,
    "commit": SessionCommitResult,
    "task": StoredTask,
    "execution": StoredExecution,
    "usage": ProviderUsageEntry,
    "usage_event": UsageLedgerEvent,
}


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


def _usage_v1_to_v2(value: dict[str, Any]) -> None:
    _add(value, "plugin_id", None)


def _usage_event_v1_to_v2(value: dict[str, Any]) -> None:
    """The envelope is unchanged; its entry has an independent schema conversion."""


VERSION_STEPS: dict[str, dict[int, UpgradeStep]] = {
    "context": {3: UpgradeStep(4, _context_v3_to_v4)},
    "run": {2: UpgradeStep(5, _run_v2_to_v5)},
    "message": {3: UpgradeStep(4, _message_v3_to_v4)},
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
    if kind == "context":
        for item in value.get("source_messages", ()):
            yield "message", item
    elif kind == "commit":
        yield "session", value["session"]
        for item in value.get("messages", ()):
            yield "message", item
        for item in value.get("events", ()):
            yield "event", item
        if value.get("truncation") is not None:
            for key in ("first_removed", "last_retained"):
                if value["truncation"].get(key) is not None:
                    yield "message", value["truncation"][key]
    elif kind == "usage_event":
        if value.get("entry") is not None:
            yield "usage", value["entry"]


def _legacy_tool_results(kind: str, value: dict[str, Any]) -> Iterator[dict[str, Any]]:
    # Injected model messages have no independent schema version.
    if kind == "context":
        for message in value.get("injected_messages", ()):
            result = message.get("tool_result")
            if result is not None and "producer" not in result:
                yield result


def _needs_upgrade(kind: str, value: dict[str, Any]) -> bool:
    version = value.get("schema_version")
    if type(version) is not int or version != MODELS[kind].model_fields["schema_version"].default:
        # The transition executor rejects unsupported versions before transforming.
        return True
    return any(_needs_upgrade(child, raw) for child, raw in _nested(kind, value)) or any(
        True for _ in _legacy_tool_results(kind, value)
    )


def _convert_nested(kind: str, value: dict[str, Any]) -> None:
    for child, raw in _nested(kind, value):
        _convert_owned(child, raw)
    for result in _legacy_tool_results(kind, value):
        _producer(result)


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
