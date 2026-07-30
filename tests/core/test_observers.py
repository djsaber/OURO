import asyncio

import pytest

from ouro.application.observers import ObserverDispatcher
from ouro.core.base import utc_now
from ouro.core.observers import (
    ContextPreparedObservation,
    ObserverFailure,
    ObserverRegistration,
    ObserverRegistry,
    RuntimeEventObservation,
    RuntimeEventView,
)


def _context_observation(run_id: str = "run_1") -> ContextPreparedObservation:
    return ContextPreparedObservation(
        session_id="ses_1",
        run_id=run_id,
        message_count=1,
        tool_count=2,
        model_round=0,
    )


def test_observer_registry_orders_by_priority_and_identity_not_provenance() -> None:
    registry = ObserverRegistry()

    def handler(_observation) -> None:
        return None

    registry.register_many(
        (
            ObserverRegistration(
                plugin_id="official",
                observer_id="late",
                event="context_prepared",
                source="official",
                priority=100,
                handler=handler,
            ),
            ObserverRegistration(
                plugin_id="third",
                observer_id="early",
                event="context_prepared",
                source="third_party",
                priority=0,
                handler=handler,
            ),
        )
    )

    assert [item.observer_id for item in registry.registered("context_prepared")] == [
        "early",
        "late",
    ]


def test_runtime_event_filter_is_exact_and_empty_means_all() -> None:
    registry = ObserverRegistry()

    def handler(_observation) -> None:
        return None

    registry.register_many(
        (
            ObserverRegistration(
                plugin_id="filtered",
                observer_id="runs",
                event="runtime_event",
                event_types=("run_started",),
                handler=handler,
            ),
            ObserverRegistration(
                plugin_id="all",
                observer_id="all",
                event="runtime_event",
                handler=handler,
            ),
        )
    )
    observation = RuntimeEventObservation(
        session_id="ses_1",
        run_id="run_1",
        record=RuntimeEventView(
            event_id="evt_1",
            session_id="ses_1",
            run_id="run_1",
            turn_id=None,
            event_type="tool_call_started",
            created_at=utc_now(),
            sequence=1,
        ),
    )

    assert [item.observer_id for item in registry.matching(observation)] == ["all"]


@pytest.mark.asyncio
async def test_slow_observer_does_not_block_sibling_or_enqueue() -> None:
    registry = ObserverRegistry()
    slow_started = asyncio.Event()
    release_slow = asyncio.Event()
    fast_finished = asyncio.Event()

    async def slow(_observation) -> None:
        slow_started.set()
        await release_slow.wait()

    async def fast(_observation) -> None:
        fast_finished.set()

    registry.register_many(
        (
            ObserverRegistration(
                plugin_id="slow",
                observer_id="slow",
                event="context_prepared",
                handler=slow,
            ),
            ObserverRegistration(
                plugin_id="fast",
                observer_id="fast",
                event="context_prepared",
                handler=fast,
            ),
        )
    )
    dispatcher = ObserverDispatcher()

    dispatcher.enqueue(registry, _context_observation())
    await asyncio.wait_for(slow_started.wait(), timeout=0.2)
    await asyncio.wait_for(fast_finished.wait(), timeout=0.2)
    release_slow.set()
    await dispatcher.close()


@pytest.mark.asyncio
async def test_failure_is_run_scoped_and_does_not_disable_sibling() -> None:
    registry = ObserverRegistry()
    calls: list[str] = []
    failures: list[ObserverFailure] = []
    releases = 0

    def release() -> None:
        nonlocal releases
        releases += 1

    def fail(_observation) -> None:
        calls.append("fail")
        raise RuntimeError("token=observer-secret")

    def succeed(_observation) -> None:
        calls.append("succeed")

    registry.register_many(
        (
            ObserverRegistration(
                plugin_id="dev.ouro.observers",
                observer_id="fail",
                event="context_prepared",
                handler=fail,
            ),
            ObserverRegistration(
                plugin_id="dev.ouro.observers",
                observer_id="succeed",
                event="context_prepared",
                handler=succeed,
            ),
        )
    )
    dispatcher = ObserverDispatcher()
    dispatcher.set_failure_sink(failures.append)

    dispatcher.enqueue(registry, _context_observation(), release=release)
    await dispatcher.drain()
    dispatcher.enqueue(registry, _context_observation(), release=release)
    await dispatcher.drain()
    dispatcher.finish_run("run_1")

    assert calls == ["fail", "succeed", "succeed"]
    assert releases == 2
    assert len(failures) == 1
    assert failures[0].message == "token=***"

    dispatcher.enqueue(registry, _context_observation(), release=release)
    await dispatcher.close()
    assert calls.count("fail") == 2
    assert calls.count("succeed") == 3
    assert releases == 3


@pytest.mark.asyncio
async def test_queue_overflow_isolated_and_releases_every_delivery() -> None:
    registry = ObserverRegistry()
    failures: list[ObserverFailure] = []
    slow_started = asyncio.Event()
    release_slow = asyncio.Event()
    fast_calls: list[int] = []
    fast_received = [asyncio.Event() for _ in range(3)]
    releases = 0

    def release() -> None:
        nonlocal releases
        releases += 1

    async def slow(_observation) -> None:
        slow_started.set()
        await release_slow.wait()

    def fast(observation) -> None:
        fast_calls.append(observation.model_round)
        fast_received[observation.model_round].set()

    registry.register_many(
        (
            ObserverRegistration(
                plugin_id="slow",
                observer_id="slow",
                event="context_prepared",
                handler=slow,
            ),
            ObserverRegistration(
                plugin_id="fast",
                observer_id="fast",
                event="context_prepared",
                handler=fast,
            ),
        )
    )
    dispatcher = ObserverDispatcher(mailbox_size=1)
    dispatcher.set_failure_sink(failures.append)

    for model_round in range(3):
        dispatcher.enqueue(
            registry,
            ContextPreparedObservation(
                session_id="ses_1",
                run_id="run_1",
                message_count=1,
                tool_count=2,
                model_round=model_round,
            ),
            release=release,
        )
        if model_round == 0:
            await asyncio.wait_for(slow_started.wait(), timeout=0.2)
        await asyncio.wait_for(fast_received[model_round].wait(), timeout=0.2)

    release_slow.set()
    await dispatcher.close()

    assert fast_calls == [0, 1, 2]
    assert releases == 3
    assert [failure.error_type for failure in failures] == ["ObserverQueueFull"]


@pytest.mark.asyncio
async def test_shutdown_cancels_stuck_observer_and_releases_lease() -> None:
    registry = ObserverRegistry()
    started = asyncio.Event()
    never = asyncio.Event()
    releases = 0

    async def stuck(_observation) -> None:
        started.set()
        await never.wait()

    def release() -> None:
        nonlocal releases
        releases += 1

    registry.register(
        ObserverRegistration(
            plugin_id="stuck",
            observer_id="stuck",
            event="context_prepared",
            handler=stuck,
            timeout_seconds=30,
        )
    )
    dispatcher = ObserverDispatcher(shutdown_timeout_seconds=0.01)
    dispatcher.enqueue(registry, _context_observation(), release=release)
    dispatcher.enqueue(registry, _context_observation(), release=release)
    await asyncio.wait_for(started.wait(), timeout=0.2)

    await dispatcher.close()

    assert releases == 2
