from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Sequence, Set
from typing import Any

from pydantic import TypeAdapter, ValidationError

from ouro.core.errors import PersistenceError
from ouro.core.message_content import validate_tool_message
from ouro.core.messages import MessageRecord
from ouro.core.persistence import decode_versioned_record_json
from ouro.core.session_commits import SessionCommit
from ouro.core.state import ACTIVE_RUN_STATUSES, SessionRecord
from ouro.core.tool_batches import ToolCallState, validate_call_states
from ouro.core.tool_facts import (
    ToolCallFacts,
    ToolCallIdentity,
    ToolCallRecord,
    ToolCallStart,
    ToolOutcomeRecord,
)
from ouro.sessions.sqlite_runs import read_run

_STRUCTURED_RESULT = TypeAdapter(dict[str, Any])


def _restore_structured(record: ToolOutcomeRecord, payload: str | bytes) -> None:
    try:
        record.value.structured = _STRUCTURED_RESULT.validate_json(payload)
    except ValidationError as exc:
        raise PersistenceError(f"could not decode Outcome structured result: {exc}") from exc


def read_fact[T: (ToolCallRecord, ToolCallStart, ToolOutcomeRecord)](
    connection: sqlite3.Connection,
    session_id: str,
    identity: ToolCallIdentity,
    record_type: type[T],
    *,
    attempt: int | None = None,
    include_structured: bool = True,
    include_arguments: bool = True,
) -> T | None:
    """Read a fact; internal consumers can omit unneeded arguments or structured data."""
    table = _table(record_type)
    values: tuple[object, ...] = (
        session_id,
        identity.run_id,
        identity.batch_id,
        identity.tool_call_id,
    )
    where = "session_id = ? AND run_id = ? AND batch_id = ? AND tool_call_id = ?"
    if record_type is not ToolCallRecord:
        if attempt is None or attempt < (1 if record_type is ToolCallStart else 0):
            raise ValueError("invalid Call attempt")
        where += " AND attempt = ?"
        values += (attempt,)
    columns = (
        "payload, structured"
        if record_type is ToolOutcomeRecord and include_structured
        else "payload"
    )
    if record_type is ToolCallRecord and not include_arguments:
        columns = "projection"
    row = connection.execute(f"SELECT {columns} FROM {table} WHERE {where}", values).fetchone()
    if row is None:
        return None
    result = decode_versioned_record_json(record_type, row[0], label=table)
    if isinstance(result, ToolOutcomeRecord):
        if include_structured:
            _restore_structured(result, row[1])
        result.validate_stored_content()
    if result.session_id != session_id or result.identity != identity:
        raise PersistenceError("Call fact has a foreign identity")
    if isinstance(result, ToolCallStart | ToolOutcomeRecord) and result.attempt != attempt:
        raise PersistenceError("Call fact belongs to another attempt")
    return result


def write_fact(
    connection: sqlite3.Connection,
    record: ToolCallRecord | ToolCallStart | ToolOutcomeRecord,
) -> None:
    table = _table(type(record))
    columns = "session_id, run_id, batch_id, tool_call_id, payload"
    values: tuple[object, ...] = (
        record.session_id,
        record.identity.run_id,
        record.identity.batch_id,
        record.identity.tool_call_id,
        record.model_dump_json(exclude={"value": {"structured"}})
        if isinstance(record, ToolOutcomeRecord)
        else record.model_dump_json(),
    )
    if isinstance(record, ToolCallRecord):
        columns += ", projection"
        values += (record.model_dump_json(exclude={"request": {"arguments"}}),)
    if isinstance(record, ToolCallStart | ToolOutcomeRecord):
        columns += ", attempt"
        values += (record.attempt,)
    if isinstance(record, ToolOutcomeRecord):
        columns += ", structured"
        values += (_STRUCTURED_RESULT.dump_json(record.value.structured).decode(),)
    placeholders = ", ".join("?" for _ in values)
    connection.execute(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", values)


def write_facts(
    connection: sqlite3.Connection,
    records: Sequence[ToolCallRecord | ToolCallStart | ToolOutcomeRecord],
) -> None:
    for record in records:
        write_fact(connection, record)


def read_selected[T: (ToolCallRecord, ToolOutcomeRecord)](
    connection: sqlite3.Connection,
    record_type: type[T],
    keys: Set[tuple[str | int, ...]],
    *,
    include_arguments: bool = True,
) -> dict[tuple[str | int, ...], T]:
    """Read one selected batch, with Call arguments only when the batch uses requests."""
    if not keys:
        return {}
    fields = ["session_id", "run_id", "batch_id", "tool_call_id"]
    if record_type is ToolOutcomeRecord:
        fields.append("attempt")
    joins = " AND ".join(
        f"fact.{field} = json_extract(requested.value, '$[{index}]')"
        for index, field in enumerate(fields)
    )
    payload = (
        "fact.projection"
        if record_type is ToolCallRecord and not include_arguments
        else "fact.payload"
    )
    rows = connection.execute(
        f"SELECT {payload}, {', '.join('fact.' + name for name in fields)} "
        f"FROM json_each(?) AS requested JOIN {_table(record_type)} AS fact ON {joins}",
        (json.dumps(tuple(keys), ensure_ascii=False),),
    )
    result: dict[tuple[str | int, ...], T] = {}
    for row in rows:
        record = decode_versioned_record_json(record_type, row[0], label="selected Call fact")
        identity = record.identity
        key: tuple[str | int, ...] = (
            record.session_id,
            identity.run_id,
            identity.batch_id,
            identity.tool_call_id,
        )
        if isinstance(record, ToolOutcomeRecord):
            key += (record.attempt,)
            record.validate_stored_content()
        if key != row[1:]:
            raise PersistenceError("selected Call fact has a foreign identity or attempt")
        result[key] = record
    return result


def _table(record_type: type[ToolCallRecord | ToolCallStart | ToolOutcomeRecord]) -> str:
    if record_type is ToolCallRecord:
        return "tool_calls"
    if record_type is ToolCallStart:
        return "tool_call_starts"
    if record_type is ToolOutcomeRecord:
        return "tool_outcomes"
    raise TypeError("unknown Call fact type")


def read_facts[T: (ToolCallRecord, ToolCallStart, ToolOutcomeRecord)](
    connection: sqlite3.Connection, session_id: str, record_type: type[T]
) -> Iterator[T]:
    order = "run_id, batch_id, tool_call_id"
    if record_type is not ToolCallRecord:
        order += ", attempt"
    columns = f"payload, {order}"
    if record_type is ToolOutcomeRecord:
        columns += ", structured"
    for row in connection.execute(
        f"SELECT {columns} FROM {_table(record_type)} WHERE session_id = ? ORDER BY {order}",
        (session_id,),
    ):
        record = decode_versioned_record_json(record_type, row[0], label="Call snapshot")
        if record.session_id != session_id or record.identity != ToolCallIdentity(
            run_id=row[1], batch_id=row[2], tool_call_id=row[3]
        ):
            raise PersistenceError("Call snapshot has a foreign identity")
        if isinstance(record, ToolCallStart | ToolOutcomeRecord) and record.attempt != row[4]:
            raise PersistenceError("Call snapshot belongs to another attempt")
        if isinstance(record, ToolOutcomeRecord):
            _restore_structured(record, row[-1])
            record.validate_stored_content()
        yield record


def load_snapshot(connection: sqlite3.Connection, session_id: str) -> ToolCallFacts:
    result = ToolCallFacts(
        calls=tuple(read_facts(connection, session_id, ToolCallRecord)),
        starts=tuple(read_facts(connection, session_id, ToolCallStart)),
        outcomes=tuple(read_facts(connection, session_id, ToolOutcomeRecord)),
    )
    result.validate_snapshot(session_id)
    return result


def read_call_state(
    connection: sqlite3.Connection, session_id: str, identity: ToolCallIdentity
) -> ToolCallState | None:
    row = connection.execute(
        "SELECT payload FROM tool_call_states "
        "WHERE session_id = ? AND run_id = ? AND batch_id = ? AND tool_call_id = ?",
        (session_id, identity.run_id, identity.batch_id, identity.tool_call_id),
    ).fetchone()
    if row is None:
        return None
    state = decode_versioned_record_json(ToolCallState, row[0], label="Call state")
    if state.session_id != session_id or state.identity != identity:
        raise PersistenceError("Call state has a foreign identity")
    return state


def write_call_state(
    connection: sqlite3.Connection, state: ToolCallState, *, revision: int = 0
) -> None:
    connection.execute(
        "INSERT INTO tool_call_states(session_id, run_id, batch_id, tool_call_id, "
        "payload, modified_revision) "
        "VALUES(?, ?, ?, ?, ?, ?) ON CONFLICT(session_id, run_id, batch_id, tool_call_id) "
        "DO UPDATE SET payload = excluded.payload, modified_revision = excluded.modified_revision",
        (
            state.session_id,
            state.identity.run_id,
            state.identity.batch_id,
            state.identity.tool_call_id,
            state.model_dump_json(),
            revision,
        ),
    )


def load_states(connection: sqlite3.Connection, session_id: str) -> tuple[ToolCallState, ...]:
    states = []
    for row in connection.execute(
        "SELECT payload, run_id, batch_id, tool_call_id FROM tool_call_states "
        "WHERE session_id = ? ORDER BY run_id, batch_id, tool_call_id",
        (session_id,),
    ):
        state = decode_versioned_record_json(ToolCallState, row[0], label="Call state snapshot")
        if state.session_id != session_id or state.identity != ToolCallIdentity(
            run_id=row[1], batch_id=row[2], tool_call_id=row[3]
        ):
            raise PersistenceError("Call state snapshot has a foreign identity")
        states.append(state)
    return tuple(states)


def load_required(
    connection: sqlite3.Connection,
    session_id: str,
    proposed: ToolCallFacts,
    states: tuple[ToolCallState, ...] = (),
    messages: tuple[MessageRecord, ...] = (),
) -> ToolCallFacts:
    # Equality checks require original parameters; reference binding does not.
    arguments_for = {
        *(call.identity for call in proposed.calls),
        *(start.identity for start in proposed.starts if start.arguments_override is not None),
        *(state.identity for state in states if state.arguments_override is not None),
    }
    return ToolCallFacts(
        calls=tuple(
            call
            for identity in dict.fromkeys(
                (
                    *proposed.call_identities,
                    *(s.identity for s in states),
                    *(identity for message in messages for identity in message.referenced_call_ids),
                )
            )
            if (
                call := read_fact(
                    connection,
                    session_id,
                    identity,
                    ToolCallRecord,
                    include_arguments=identity in arguments_for,
                )
            )
            is not None
        ),
        starts=tuple(
            start
            for identity, attempt in dict.fromkeys(
                (*proposed.start_keys, *((s.identity, s.attempt) for s in states if s.attempt > 0))
            )
            if (
                start := read_fact(connection, session_id, identity, ToolCallStart, attempt=attempt)
            )
            is not None
        ),
        outcomes=tuple(
            outcome
            for identity, attempt in dict.fromkeys(
                (
                    *proposed.outcome_keys,
                    *(
                        (message.outcome_ref.identity, message.outcome_ref.outcome_attempt)
                        for message in messages
                        if message.outcome_ref is not None
                        and message.outcome_ref.outcome_attempt is not None
                    ),
                    *(
                        (s.identity, s.outcome_attempt)
                        for s in states
                        if s.outcome_attempt is not None
                    ),
                )
            )
            if (
                outcome := read_fact(
                    connection, session_id, identity, ToolOutcomeRecord, attempt=attempt
                )
            )
            is not None
        ),
    )


def prepare_commit_facts(
    connection: sqlite3.Connection, request: SessionCommit, current: SessionRecord
) -> tuple[ToolCallFacts, dict[str, tuple[str, ...]]]:
    proposed = request.tool_facts
    session_id = current.session_id
    messages = tuple(step.message for step in request.steps if step.message is not None)
    existing = load_required(connection, session_id, proposed, request.call_states, messages)
    active_runs = {}
    active_run_id = (
        request.after.active_run_id
        if request.after is not None and request.after.active_run_id != request.before.active_run_id
        else current.active_run_id
    )
    staged_runs = {run.run_id: run for run in request.runs}
    for run_id in dict.fromkeys(
        (*proposed.execution_run_ids, *(state.identity.run_id for state in request.call_states))
    ):
        run = staged_runs.get(run_id)
        if run is None:
            try:
                run = read_run(connection, session_id, run_id)
            except FileNotFoundError:
                continue
        elif run.session_id != session_id:
            raise PersistenceError("Call Run has a foreign identity")
        if (
            run.run_id == active_run_id
            and run.status in ACTIVE_RUN_STATUSES
            and not run.abort_requested
        ):
            active_runs[run_id] = run.history_epoch
    prepared = proposed.prepare(
        session_id=session_id,
        history_epoch=current.history_epoch,
        active_runs=active_runs,
        existing=existing,
    )
    validate_call_states(
        request.call_states,
        ToolCallFacts(
            calls=existing.calls + prepared.calls,
            starts=existing.starts + prepared.starts,
            outcomes=existing.outcomes + prepared.outcomes,
        ),
        session_id=session_id,
        active_runs=active_runs,
        history_epoch=current.history_epoch,
    )
    calls = {call.identity: call for call in (*existing.calls, *prepared.calls)}
    outcomes = {
        (item.identity, item.attempt): item for item in (*existing.outcomes, *prepared.outcomes)
    }
    resources = {}
    for message in messages:
        reference = message.outcome_ref
        outcome = (
            outcomes.get((reference.identity, reference.outcome_attempt))
            if reference is not None and reference.outcome_attempt is not None
            else None
        )
        validate_tool_message(message.as_fact(), calls, outcome)
        resources[message.message_id] = tuple(
            outcome.value.resource_refs if outcome is not None else message.resource_refs
        )
    return prepared, resources
