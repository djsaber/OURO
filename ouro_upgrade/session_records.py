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
from ouro.core.tools import ToolCallProducer, ToolInvocation, ToolInvocationBinding, ToolResultValue
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
    for raw in raw_runs:
        value = deepcopy(raw)
        batch = value.get("active_tool_batch")
        if batch is not None:
            require(value["schema_version"] in (2, 5), "run", "unsupported historical Run")
            batch.setdefault("producer", {"kind": "model", "driver_id": None})
            batches[batch.get("assistant_message_id")] = (raw["run_id"], batch)
            members = batch.pop("calls")
            batch["call_ids"] = [item["request"]["id"] for item in members]
            head = validate(ToolBatchHead, batch)
            for item in members:
                identity = head.identity(raw["run_id"], item["request"]["id"])
                call = ToolCallRecord(
                    session_id=raw["session_id"],
                    identity=identity,
                    history_epoch=raw["history_epoch"],
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
                if invocation_raw is not None:
                    invocation_raw.setdefault("producer", batch["producer"])
                    invocation = validate(ToolInvocation, invocation_raw)
                    require(
                        invocation.tool_call_id == identity.tool_call_id
                        and invocation.run_id == identity.run_id
                        and invocation.turn_id == call.turn_id,
                        "run",
                        "execution binding identity mismatch",
                    )
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
                        require(
                            result.get("ended_at") is not None,
                            "run",
                            "unfinished historical result",
                        )
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
                        proposals = {
                            key: result[key]
                            for key in ("harness_change", "session_control_transitions")
                            if result.get(key)
                        }
                        if proposals and item.get("result_message_id") is None:
                            state["proposals"] = proposals
                else:
                    require(result is None, "run", "result has no execution binding")
                require(
                    item["status"] != "running",
                    "run",
                    "in-flight execution requires original recovery",
                )
                states.append(validate(ToolCallState, state))
            value["active_tool_batch"] = None
        run = validate(RunRecord, convert("run", value))
        if batch is not None:
            run.active_tool_batch = head
        runs.append(run)
    for identity, call in calls.items():
        if call.producer.kind == "driver":
            by_request[(identity.run_id, identity.tool_call_id)] = identity
    messages = []
    for raw in raw_messages:
        require(raw.get("schema_version") in (3, 4), "message", "unsupported historical Message")
        value = deepcopy(raw)
        requests = value.pop("tool_calls", [])
        result = value.pop("tool_result", None)
        value["schema_version"] = 8
        if requests:
            require(bool(value.get("run_id")), "message", "tool calls require a Run identity")
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
            require(result_identity is not None, "message", "tool result has no preceding Call")
            assert result_identity is not None
            identity = result_identity
            require(
                calls[identity].request.name == result["tool_name"],
                "message",
                "tool result identity mismatch",
            )
            value["outcome_ref"] = {"identity": identity, "feedback_is_error": result["is_error"]}
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
        value.pop("source_digest")
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
    value = convert("session", raw)
    value = {**value, "run_count": len(runs), "resource_count": len(resources)}
    record = validate(SessionRecord, value)
    require(record.message_count == len(messages), "session", "message count mismatch")
    return record
