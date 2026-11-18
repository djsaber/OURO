"""Resolve a selected message range with bounded native fact joins."""

from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Iterable, Iterator
from itertools import batched

from ouro.core.inputs import InputRecord
from ouro.core.message_content import materialize_owned_message
from ouro.core.messages import MessageRecord
from ouro.core.tool_facts import ToolCallRecord, ToolOutcomeRecord
from ouro.sessions.sqlite_facts import read_selected
from ouro.sessions.sqlite_inputs import read_inputs


def _messages_with_inputs(
    connection: sqlite3.Connection, batch: tuple[MessageRecord, ...]
) -> Iterator[tuple[MessageRecord, InputRecord | None]]:
    identities = {item.input_ref.input_id for item in batch if item.input_ref is not None}
    inputs = {
        record.item.queue_id: record
        for record in read_inputs(connection, batch[0].session_id, identities)
    }
    for item in batch:
        yield item, inputs.get(item.input_ref.input_id) if item.input_ref is not None else None


def resolve_messages(
    messages: Iterable[MessageRecord],
    *,
    connection: sqlite3.Connection,
) -> Iterator[MessageRecord]:
    """Join each batch once; transfer source units unless another result needs them.

    The returned messages never share mutable parameters, producer metadata or
    result bodies. Input views own their selected skill and attachment values;
    tool units transfer directly unless reused. No facts are cached across batches.
    """
    for batch in batched(messages, 64, strict=False):
        selected_inputs = _messages_with_inputs(connection, batch)
        if not any(item.call_refs or item.outcome_ref is not None for item in batch):
            for item, record in selected_inputs:
                yield materialize_owned_message(item, record)
            continue
        requests = Counter(
            (item.session_id, identity.run_id, identity.batch_id, identity.tool_call_id)
            for item in batch
            for identity in item.call_refs
        )
        results = Counter(
            (
                item.session_id,
                item.outcome_ref.identity.run_id,
                item.outcome_ref.identity.batch_id,
                item.outcome_ref.identity.tool_call_id,
            )
            for item in batch
            if item.outcome_ref is not None
        )
        observations = Counter(
            (
                item.session_id,
                item.outcome_ref.identity.run_id,
                item.outcome_ref.identity.batch_id,
                item.outcome_ref.identity.tool_call_id,
                item.outcome_ref.outcome_attempt,
            )
            for item in batch
            if item.outcome_ref is not None and item.outcome_ref.outcome_attempt is not None
        )
        keys = set(requests) | set(results)
        calls = read_selected(connection, ToolCallRecord, keys, include_arguments=bool(requests))
        outcomes = read_selected(connection, ToolOutcomeRecord, set(observations))
        for item, record in selected_inputs:
            selected = []
            for identity in item.referenced_call_ids:
                key = (item.session_id, identity.run_id, identity.batch_id, identity.tool_call_id)
                call = calls.get(key)
                if call is None:
                    continue
                if item.call_refs:
                    requests[key] -= 1
                    if requests[key]:
                        call = call.model_copy(
                            update={"request": call.request.model_copy(deep=True)}
                        )
                else:
                    results[key] -= 1
                    if results[key]:
                        call = call.model_copy(update={"producer": call.producer.model_copy()})
                selected.append(call)
            outcome = None
            reference = item.outcome_ref
            if reference is not None and reference.outcome_attempt is not None:
                observed = (
                    item.session_id,
                    reference.identity.run_id,
                    reference.identity.batch_id,
                    reference.identity.tool_call_id,
                    reference.outcome_attempt,
                )
                outcome = outcomes.get(observed)
                observations[observed] -= 1
                if outcome is not None and observations[observed]:
                    outcome = outcome.model_copy(deep=True)
            yield materialize_owned_message(
                item,
                record,
                calls=selected,
                outcome=outcome,
            )
