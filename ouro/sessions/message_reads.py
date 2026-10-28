"""Resolve a selected message range with bounded native fact joins."""

from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from itertools import batched

from ouro.core.inputs import InputRecord
from ouro.core.message_content import materialize_owned_message
from ouro.core.messages import MessageRecord
from ouro.core.tool_facts import ToolCallRecord, ToolOutcomeRecord
from ouro.sessions.sqlite_facts import read_selected


def resolve_messages(
    messages: Iterable[MessageRecord],
    *,
    connection: sqlite3.Connection,
    read_input: Callable[[str, str], InputRecord | None],
) -> Iterator[MessageRecord]:
    """Join each batch once; transfer source units unless another result needs them.

    The returned messages never share mutable parameters, producer metadata or
    result bodies. A source used once transfers directly; only repeated views
    copy that source unit. No decoded facts survive the current batch.
    """
    for batch in batched(messages, 64, strict=False):
        if not any(item.call_refs or item.outcome_ref is not None for item in batch):
            for item in batch:
                yield materialize_owned_message(
                    item,
                    read_input(item.session_id, item.input_ref.input_id)
                    if item.input_ref is not None
                    else None,
                )
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
        for item in batch:
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
                read_input(item.session_id, item.input_ref.input_id)
                if item.input_ref is not None
                else None,
                calls=selected,
                outcome=outcome,
            )
