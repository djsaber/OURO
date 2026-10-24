from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from ouro.application.contracts.execution import RuntimeStatusSnapshot
from ouro.application.supervisor import RuntimeSupervisor
from ouro.core.callbacks import run_blocking
from ouro.core.results import RunResult
from ouro.core.state import RunRecord


async def wait_for_runtime_event(
    event: asyncio.Event,
    task: asyncio.Task[RunResult] | asyncio.Task[RuntimeStatusSnapshot] | asyncio.Task[RunRecord],
    *,
    description: str,
) -> None:
    """Await a run's phase handshake, surfacing early termination immediately.

    Startup includes real storage and plugin processes. The deadline guards a
    hang rather than imposing a latency budget on those dependencies.
    The caller retains ownership of the run and its test gates.
    """
    handshake = asyncio.create_task(event.wait())
    try:
        try:
            async with asyncio.timeout(60):
                await asyncio.wait((handshake, task), return_when=asyncio.FIRST_COMPLETED)
        except TimeoutError:
            raise AssertionError(f"Timed out waiting for {description}; task={task!r}") from None
        if not event.is_set():
            result = task.result()
            if isinstance(result, RunResult):
                result = result.run
            outcome = (
                f"{result.status}; {result.error}"
                if isinstance(result, RunRecord)
                else f"{result.status}; {result.diagnostics}"
            )
            raise AssertionError(f"Run ended before {description}: {outcome}")
    finally:
        handshake.cancel()
        await asyncio.gather(handshake, return_exceptions=True)


async def wait_for_run_event(event, supervisor, session_id, run_id, *, description):
    """Observe a known durable Run without taking ownership of its execution."""
    completion = asyncio.create_task(supervisor.wait_run(session_id, run_id))
    try:
        await wait_for_runtime_event(event, completion, description=description)
    finally:
        completion.cancel()
        await asyncio.gather(completion, return_exceptions=True)


async def wait_for_activity(subscription, *, status):
    """Await the observer's own projection, including an already-current snapshot."""
    async with asyncio.timeout(60):
        activity = subscription.initial_activity
        while activity is None or activity.status != status:
            notification = await subscription.get()
            assert notification.diagnostic is None, notification.diagnostic
            if notification.activity is not None:
                activity = notification.activity
        return activity


async def wait_for_session_state(supervisor, session_id, read, *, event_types=frozenset()):
    """Read a durable predicate only at subscription invalidations, off-loop."""
    subscription = None
    try:
        async with asyncio.timeout(60):
            subscription = await supervisor.subscribe_state(session_id, event_types=event_types)
            while True:
                result = await run_blocking(read)
                if result:
                    return result
                notification = await subscription.get()
                assert notification.diagnostic is None, notification.diagnostic
    finally:
        if subscription is not None:
            subscription.close()


async def wait_for_agent_interactions(service, root_id, *, count=1):
    """Wait for the canonical interaction projection, not a polling deadline."""
    subscription = None
    try:
        async with asyncio.timeout(60):
            subscription = await service.subscribe_agent_tree(root_id)
            snapshot = subscription.initial_snapshot
            while True:
                if snapshot is not None and snapshot.pending_interaction_count == count:
                    items = await run_blocking(service.list_agent_interactions, root_id)
                    if len(items) == count:
                        return items
                notification = await subscription.get()
                assert notification.diagnostic is None, notification.diagnostic
                snapshot = notification.snapshot
    finally:
        if subscription is not None:
            subscription.close()


async def wait_for_parent_delivery(
    supervisor: RuntimeSupervisor, session_id: str, run_id: str
) -> None:
    """Observe durable delivery independently of parent execution completion.

    Subscribe before reading so a delivery committed during the read cannot be
    missed. Read only on invalidation, outside the event loop; polling the file
    store competes with the very child and parent writes being awaited.
    """

    subscription = None
    relations = ()
    delivered = set()
    sequence = 0
    try:
        async with asyncio.timeout(30):
            subscription = await supervisor.subscribe_state(
                session_id, event_types=frozenset({"agent_result_delivered"})
            )
            while True:
                relations = tuple(
                    relation
                    for relation in await asyncio.to_thread(
                        supervisor.store.list_delegations, session_id
                    )
                    if relation.parent_run_id == run_id and relation.execution_mode == "sync"
                )
                events, more = await asyncio.to_thread(
                    supervisor.store.load_events_after, session_id, after_sequence=sequence
                )
                for event in events:
                    sequence = event.sequence
                    if event.run_id == run_id and event.event_type == "agent_result_delivered":
                        delivered.add(event.payload["delegation_id"])
                if relations and all(
                    relation.result is not None
                    and relation.result.delivery.status == "delivered"
                    and relation.delegation_id in delivered
                    for relation in relations
                ):
                    return
                if more:
                    continue
                notification = await subscription.get()
                assert notification.diagnostic is None, notification.diagnostic
    except TimeoutError:
        deliveries = [
            (relation.delegation_id, relation.result.delivery.status if relation.result else None)
            for relation in relations
        ]
        raise AssertionError(
            f"Parent delivery did not settle: session={session_id}, run={run_id}, "
            f"deliveries={deliveries}, published={delivered}"
        ) from None
    finally:
        if subscription is not None:
            subscription.close()


@asynccontextmanager
async def running_runtime_task(runtime, operation):
    """Own a gated run through assertions, including failure-path cleanup.

    Events and persisted state establish ordering. The overall deadline only
    detects a deadlock; individual disk operations have no latency contract.
    """
    task = asyncio.create_task(operation)
    try:
        async with asyncio.timeout(30):
            yield task
    except TimeoutError:
        raise AssertionError(f"Runtime scenario did not settle; task={task!r}") from None
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await runtime.close()
