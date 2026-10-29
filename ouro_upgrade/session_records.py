"""Translate historical Session facts directly into the current native records."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from ouro.core.compaction import CompactionRecord
from ouro.core.context_snapshots import ModelRequestHeader, RequestManifestRecord
from ouro.core.messages import MessageRecord
from ouro.core.request_manifest import RequestManifest, RequestMessage
from ouro.core.state import RunRecord, SessionRecord
from ouro.core.tool_batches import ToolBatchHead, ToolCallState
from ouro.core.tool_facts import ToolCallIdentity, ToolCallRecord, ToolCallStart, ToolOutcomeRecord
from ouro.core.tools import (
    ToolCallProducer,
    ToolCallRequest,
    ToolInvocation,
    ToolInvocationBinding,
    ToolResultValue,
)
from ouro_upgrade.errors import require
from ouro_upgrade.records import _context_metrics, convert, validate


def messages_and_runs(
    raw_messages: list[dict[str, Any]], raw_runs: list[dict[str, Any]], epoch: int
):
    calls = {}
    by_request: dict[tuple[str | None, str], ToolCallIdentity] = {}
    states = []
    execution_facts: list[ToolCallStart | ToolOutcomeRecord] = []
    batches = {}
    runs = []
    run_times: dict[str | None, str] = {}
    for message in raw_messages:
        if message.get("run_id") and message.get("created_at"):
            run_times.setdefault(message["run_id"], message["created_at"])
    for raw in raw_runs:
        value = deepcopy(raw)
        value.setdefault("started_at", run_times.get(value.get("run_id"), "1970-01-01T00:00:00Z"))
        run_times.setdefault(value["run_id"], value["started_at"])
        value.setdefault("history_epoch", epoch)
        value.setdefault("status", "recoverable")
        batch = value.get("active_tool_batch")
        if batch is not None:
            require(value["schema_version"] in (2, 5), "run", "unsupported historical Run")
            batch.setdefault("batch_id", batch.get("assistant_message_id") or value["run_id"])
            batch.setdefault("turn_id", batch.get("assistant_message_id") or value["run_id"])
            batch.setdefault("created_at", value["started_at"])
            batch.setdefault("updated_at", batch["created_at"])
            batch.setdefault("producer", {"kind": "model", "driver_id": None})
            batches[batch.get("assistant_message_id")] = (raw["run_id"], batch)
            members = batch.pop("calls")
            batch["call_ids"] = [item["request"]["id"] for item in members]
            head = validate(ToolBatchHead, batch)
            for item in members:
                identity = ToolCallIdentity(raw["run_id"], head.batch_id, item["request"]["id"])
                call = ToolCallRecord(
                    session_id=raw["session_id"],
                    identity=identity,
                    history_epoch=value["history_epoch"],
                    turn_id=head.turn_id,
                    producer=head.producer,
                    request=item.pop("request"),
                    accepted_at=head.created_at,
                )
                calls[identity] = call
                invocation_raw = item.pop("invocation", None)
                result = item.pop("result", None)
                state = {
                    "schema_version": 1,
                    "session_id": raw["session_id"],
                    "identity": identity,
                    **item,
                }
                state.setdefault("status", "planned")
                approval = state.get("approval")
                override = (
                    approval.pop("modified_arguments", None) if approval is not None else None
                )
                if invocation_raw is not None or result is not None or override is not None:
                    invocation_raw = invocation_raw or {}
                    invocation_raw.update(
                        tool_call_id=identity.tool_call_id,
                        session_id=call.session_id,
                        run_id=identity.run_id,
                        turn_id=call.turn_id,
                        tool_name=call.request.name,
                        producer=batch["producer"],
                    )
                    invocation_raw.setdefault("requested_at", batch["created_at"])
                    invocation_raw.setdefault(
                        "arguments", override if override is not None else call.request.arguments
                    )
                    if result is None and override is not None:
                        invocation_raw["arguments"] = override
                    invocation = validate(ToolInvocation, invocation_raw)
                    binding = ToolInvocationBinding.model_validate(
                        invocation.model_dump(exclude={"arguments"})
                    )
                    arguments = (
                        None
                        if invocation.arguments == call.request.arguments
                        else invocation.arguments
                    )
                    if result is None:
                        state.update(prepared_binding=binding, arguments_override=arguments)
                    else:
                        result.update(
                            tool_call_id=identity.tool_call_id, tool_name=call.request.name
                        )
                        result["started_at"] = (
                            result.get("started_at") or invocation_raw["requested_at"]
                        )
                        result["ended_at"] = result.get("ended_at") or batch["updated_at"]
                        start = ToolCallStart(
                            session_id=call.session_id,
                            identity=identity,
                            attempt=1,
                            binding=binding,
                            arguments_override=arguments,
                            started_at=result["started_at"],
                        )
                        start.validate_call(call)
                        outcome = ToolOutcomeRecord(
                            session_id=call.session_id,
                            identity=identity,
                            attempt=1,
                            value=ToolResultValue.model_validate(
                                {
                                    key: value
                                    for key, value in result.items()
                                    if key not in {"harness_change", "session_control_transitions"}
                                }
                            ),
                            observed_at=result["ended_at"],
                        )
                        outcome.validate_call(call)
                        execution_facts.extend((start, outcome))
                        state.update(attempt=1, outcome_attempt=1)
                        if state["status"] == "running":
                            state["status"] = "failed" if outcome.value.is_error else "completed"
                if state["status"] == "running":
                    state["status"] = "unknown_outcome"
                    head.phase = "recoverable"
                    value["status"] = "recoverable"
                states.append(validate(ToolCallState, state))
            value["active_tool_batch"] = None
        run = validate(RunRecord, convert("run", value))
        if batch is not None:
            run.active_tool_batch = head
        runs.append(run)
    for identity, call in calls.items():
        if call.producer.kind == "driver":
            by_request[(identity.run_id, identity.tool_call_id)] = identity
    messages: list[MessageRecord] = []
    for raw in raw_messages:
        require(raw.get("schema_version") in (3, 4), "message", "unsupported historical Message")
        value = deepcopy(raw)
        requests = value.pop("tool_calls", [])
        result = value.pop("tool_result", None)
        value["schema_version"] = 8
        value["sequence"] = len(messages) + 1
        value.setdefault("before_harness_version", None)
        value.setdefault("created_at", run_times.get(value.get("run_id"), "1970-01-01T00:00:00Z"))
        value.get("metadata", {}).pop("queued_input_id", None)
        if requests:
            value["run_id"] = value.get("run_id") or value["message_id"]
            batch = batches.get(value["message_id"])
            references = []
            for request in requests:
                identity = ToolCallIdentity(
                    value["run_id"],
                    batch[1]["batch_id"] if batch else value["message_id"],
                    request["id"],
                )
                existing_call = calls.get(identity)
                if existing_call is None:
                    call = ToolCallRecord(
                        session_id=value["session_id"],
                        identity=identity,
                        history_epoch=epoch,
                        turn_id=value.get("turn_id") or value["message_id"],
                        producer=ToolCallProducer(),
                        request=request,
                        accepted_at=value["created_at"],
                    )
                    calls[identity] = call
                else:
                    call = existing_call
                require(
                    call.request.model_dump(mode="json") == request,
                    "message",
                    "tool request differs from saved batch",
                )
                references.append(identity)
                by_request[(value["run_id"], request["id"])] = identity
            value["call_refs"] = references
        if result is not None:
            result_identity = by_request.get((value.get("run_id"), result["tool_call_id"]))
            if result_identity is None:
                result_identity = ToolCallIdentity(
                    value.get("run_id") or value["message_id"],
                    value["message_id"],
                    result["tool_call_id"],
                )
                calls[result_identity] = ToolCallRecord(
                    session_id=value["session_id"],
                    identity=result_identity,
                    history_epoch=epoch,
                    turn_id=value.get("turn_id") or value["message_id"],
                    producer=ToolCallProducer.model_validate(result.get("producer", {})),
                    request=ToolCallRequest(
                        id=result["tool_call_id"], name=result["tool_name"], arguments={}
                    ),
                    accepted_at=value["created_at"],
                )
            identity = result_identity
            value["run_id"] = value.get("run_id") or identity.run_id
            value["outcome_ref"] = {
                "identity": identity,
                "feedback_is_error": result.get("is_error", False),
            }
            # Published text is known; it does not prove an execution attempt or Outcome.
        messages.append(validate(MessageRecord, value))
    return messages, runs, [*calls.values(), *execution_facts], states


def compactions(raw_records, messages):
    positions = {item.message_id: item.sequence for item in messages}
    result = []
    for raw in raw_records:
        value = deepcopy(raw)
        require(value.get("schema_version") in (1, 2), "compaction", "unsupported version")
        if value["schema_version"] == 1 and value.get("context_metrics_after") is not None:
            _context_metrics(value["context_metrics_after"])
        covered = value.pop("covered_message_ids")
        require(
            bool(covered) and covered[-1] in positions, "compaction", "missing coverage endpoint"
        )
        value.update(
            schema_version=3,
            covered_end_message_id=covered[-1],
            covered_message_count=positions[covered[-1]],
        )
        value.pop("source_digest", None)
        value.pop("prompt_hash", None)
        result.append(validate(CompactionRecord, value))
    return result


def context(raw):
    if raw is None:
        return None, None
    value = convert("context", raw)
    header = validate(
        ModelRequestHeader,
        {key: value[key] for key in ModelRequestHeader.model_fields if key in value},
    )
    body = RequestManifestRecord(
        context_id=header.context_id,
        **{
            key: value[key]
            for key in ("supports_image_input", "supports_audio_input", "supports_video_input")
        },
        manifest=RequestManifest(
            system_prompt=value["provider_context"]["system_prompt"],
            messages=[
                RequestMessage.model_validate(item)
                for item in value["provider_context"]["messages"]
            ],
            tools=value["provider_context"]["tools"],
        ),
    )
    return header, body


def session(raw, runs, messages, resources):
    source = dict(raw)
    timestamp = (
        source.get("created_at")
        or source.get("updated_at")
        or (
            messages[0].created_at.isoformat()
            if messages
            else runs[0].started_at.isoformat()
            if runs
            else "1970-01-01T00:00:00Z"
        )
    )
    for field in ("created_at", "updated_at", "last_activity_at"):
        source[field] = source.get(field) or timestamp
    source.setdefault("history_epoch", 0)
    source.setdefault("message_count", len(messages))
    source.setdefault("status", "idle")
    source.setdefault("harness_version", "harness_upgrade")
    value = convert("session", source)
    value = {
        **value,
        "run_count": len(runs),
        "resource_count": len(resources),
        "message_count": len(messages),
        "last_message_id": messages[-1].message_id if messages else None,
    }
    if any(
        run.run_id == value.get("active_run_id") and run.status == "recoverable" for run in runs
    ):
        value.update(status="idle", active_run_id=None)
    record = validate(SessionRecord, value)
    return record
