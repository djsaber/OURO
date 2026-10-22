from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from ouro.application.supervisor import RuntimeSupervisor


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
