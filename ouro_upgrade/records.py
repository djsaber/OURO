"""Pure conversions of registered historical records; opaque JSON stays opaque."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
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
from ouro.core.tasks import TaskExecution, TaskRecord
from ouro_upgrade.errors import UpgradeError

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
VERSIONS = {
    "session": (2,),
    "run": (2, 5),
    "message": (3, 4),
    "event": (2,),
    "compaction": (1,),
    "harness": (1,),
    "delegation": (1,),
    "resource": (2,),
    "commit": (1,),
    "task": (1, 3),
    "execution": (1, 3),
    "usage": (1, 2),
    "usage_event": (1, 2),
}


def loads(text: str) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise UpgradeError("duplicate JSON key")
            result[key] = value
        return result

    try:
        value = json.loads(text, object_pairs_hook=pairs, parse_constant=_invalid_constant)
    except (ValueError, RecursionError) as exc:
        raise UpgradeError("invalid JSON object") from exc
    if not isinstance(value, dict):
        raise UpgradeError("expected JSON object")
    return value


def _invalid_constant(_: str) -> None:
    raise UpgradeError("non-finite JSON number")


def dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n"


def validate(model: type[BaseModel], value: dict[str, Any]) -> BaseModel:
    try:
        parsed = model.model_validate(value, extra="forbid")
    except ValidationError as exc:
        # Pydantic's normal message includes input values (possibly credentials).
        reason = exc.errors(include_input=False, include_context=False)[0]["type"]
        raise UpgradeError(f"invalid {model.__name__} ({reason})") from exc
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
            raise UpgradeError(f"missing persisted {type(parsed).__name__} facts")
        if isinstance(parsed, VersionedOuroModel):
            expected = type(parsed).model_fields["schema_version"].default
            if type(raw.get("schema_version")) is not int or raw["schema_version"] != expected:
                raise UpgradeError(f"missing or unsupported {type(parsed).__name__} version")
        for name, field in type(parsed).model_fields.items():
            if (
                field.default_factory is not None
                and (name.endswith(("_id", "_at", "_version")))
                and name not in raw
            ):
                raise UpgradeError(f"missing persisted {type(parsed).__name__}.{name}")
            if name in raw:
                _required_facts(getattr(parsed, name), raw[name])
    elif isinstance(parsed, (list, tuple)) and isinstance(raw, list):
        for item, source in zip(parsed, raw, strict=True):
            _required_facts(item, source)
    # Open dictionaries are intentionally not traversed.


def _add(value: dict[str, Any], key: str, default: object) -> None:
    if key in value:
        raise UpgradeError(f"unexpected field in historical contract: {key}")
    value[key] = default


def _producer(value: dict[str, Any]) -> None:
    _add(value, "producer", {"kind": "model", "driver_id": None})


def _context_v3_to_v4(value: dict[str, Any]) -> None:
    _add(value, "supports_image_input", value.pop("supports_vision"))
    _add(value, "supports_audio_input", False)
    _add(value, "supports_video_input", False)


# Each step converts its source version to the next; the model defines the target.
VERSION_STEPS: dict[str, dict[int, Callable[[dict[str, Any]], None]]] = {
    "context": {3: _context_v3_to_v4},
}


def convert(kind: str, raw: dict[str, Any]) -> dict[str, Any]:
    version = raw.get("schema_version")
    steps = VERSION_STEPS.get(kind)
    target = MODELS[kind].model_fields["schema_version"].default
    versions = (*steps, target) if steps is not None else VERSIONS[kind]
    if type(version) is not int or version not in versions:
        raise UpgradeError(f"unsupported {kind} version; expected {versions}")
    value = copy.deepcopy(raw)
    try:
        if steps is not None:
            while version < target:
                step = steps.get(version)
                if step is None:
                    raise UpgradeError(f"missing {kind} upgrade step from version {version}")
                step(value)
                version += 1
                value["schema_version"] = version
        _convert(kind, value, version)
        validate(MODELS[kind], value)
    except (KeyError, TypeError, AttributeError) as exc:
        raise UpgradeError(f"incomplete historical {kind} structure") from exc
    return value


def _convert(kind: str, value: dict[str, Any], version: int) -> None:
    if kind == "run" and version == 2:
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
        value["schema_version"] = 5
    elif kind == "message" and version == 3:
        if value.get("tool_result") is not None:
            _producer(value["tool_result"])
        value["schema_version"] = 4
    elif kind == "context":
        value["source_messages"] = [convert("message", item) for item in value["source_messages"]]
        for message in value["injected_messages"]:
            result = message.get("tool_result")
            if result is not None and "producer" not in result:
                _producer(result)
    elif kind == "commit":
        value["session"] = convert("session", value["session"])
        value["messages"] = [convert("message", item) for item in value["messages"]]
        value["events"] = [convert("event", item) for item in value["events"]]
        if value.get("truncation") is not None:
            for key in ("first_removed", "last_retained"):
                if value["truncation"].get(key) is not None:
                    value["truncation"][key] = convert("message", value["truncation"][key])
    elif kind in {"task", "execution"} and version == 1:
        record = value[kind]
        _add(record, "owner_id", None)
        if kind == "task":
            config = record["configuration"]
            target = {
                key: config.pop(key)
                for key in ("workspace", "approval_mode", "model", "reasoning", "session_mode")
            }
            target.update(kind="agent", prompt=config.pop("description"))
            _add(config, "target", target)
        else:
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
        value["schema_version"] = 3
    elif kind == "usage" and version == 1:
        _add(value, "plugin_id", None)
        value["schema_version"] = 2
    elif kind == "usage_event":
        if value.get("entry") is not None:
            value["entry"] = convert("usage", value["entry"])
        value["schema_version"] = 2


def convert_task(kind: str, raw: dict[str, Any], version: int) -> dict[str, Any]:
    """SQLite's container version selects the unversioned task payload contract."""
    value = convert(kind, {"schema_version": version, kind: raw})[kind]
    validate(TaskRecord if kind == "task" else TaskExecution, value)
    return value
