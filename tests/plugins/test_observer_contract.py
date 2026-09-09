import asyncio
import threading
from types import MappingProxyType

import pytest
from pydantic import ValidationError

from ouro.core.base import utc_now
from ouro.core.observers import (
    ContextPreparedObservation,
    Observation,
    RunFinishedObservation,
    RunOutcomeView,
    RuntimeEventObservation,
    RuntimeEventView,
    ToolCallFinishedObservation,
    ToolCallRequestedObservation,
)
from ouro.plugins.api import adapt_sdk_contributions
from ouro.plugins.manifest import PluginContributes
from ouro.plugins.worker import (
    WorkerProtocolError,
    _validate_worker_result,
    _worker_observation_payload,
)
from ouro.sdk.plugins import v2 as sdk

OBSERVATIONS: tuple[tuple[str, Observation, type[object], set[str]], ...] = (
    (
        "runtime_event",
        RuntimeEventObservation(
            session_id="ses_contract",
            run_id="run_contract",
            record=RuntimeEventView(
                event_id="evt_contract",
                session_id="ses_contract",
                run_id="run_contract",
                turn_id="turn_contract",
                event_type="run_started",
                created_at=utc_now(),
                sequence=1,
                payload=MappingProxyType({"trigger": "user"}),
            ),
        ),
        sdk.RuntimeEventObservation,
        {"event", "session_id", "run_id", "record"},
    ),
    (
        "context_prepared",
        ContextPreparedObservation(
            session_id="ses_contract",
            run_id="run_contract",
            message_count=2,
            tool_count=1,
            model_round=0,
        ),
        sdk.ContextPreparedObservation,
        {"event", "session_id", "run_id", "message_count", "tool_count", "model_round"},
    ),
    (
        "tool_call_requested",
        ToolCallRequestedObservation(
            session_id="ses_contract",
            run_id="run_contract",
            tool_call_id="call_contract",
            tool_name="Read",
            canonical_tool_id="ouro.host/tools/Read",
            owner_id="ouro.host",
            effect="read_only",
            batch_behavior="parallel",
            arguments=MappingProxyType({"path": "README.md"}),
        ),
        sdk.ToolCallRequestedObservation,
        {"event", "session_id", "run_id", "tool_call_id", "arguments"},
    ),
    (
        "tool_call_finished",
        ToolCallFinishedObservation(
            session_id="ses_contract",
            run_id="run_contract",
            tool_call_id="call_contract",
            tool_name="Read",
            canonical_tool_id="ouro.host/tools/Read",
            owner_id="ouro.host",
            effect="read_only",
            batch_behavior="parallel",
            is_error=False,
            content="done",
            structured=MappingProxyType({"ok": True}),
        ),
        sdk.ToolCallFinishedObservation,
        {"event", "session_id", "run_id", "tool_call_id", "structured"},
    ),
    (
        "run_finished",
        RunFinishedObservation(
            session_id="ses_contract",
            run_id="run_contract",
            outcome=RunOutcomeView(
                session_id="ses_contract",
                run_id="run_contract",
                status="completed",
                origin="user",
                final_text="done",
            ),
        ),
        sdk.RunFinishedObservation,
        {"event", "session_id", "run_id", "outcome"},
    ),
)


@pytest.mark.parametrize(("event", "observation", "sdk_type", "wire_fields"), OBSERVATIONS)
@pytest.mark.asyncio
async def test_observation_sdk_and_worker_payloads_cover_every_public_type(
    event: str,
    observation: Observation,
    sdk_type: type[object],
    wire_fields: set[str],
) -> None:
    seen: list[sdk.Observation] = []
    contributions = adapt_sdk_contributions(
        owner_id="example.plugin",
        contributions=sdk.ContributionSet(
            observers=[
                sdk.ObserverContribution(
                    id="facts",
                    event=event,
                    handler=seen.append,
                )
            ]
        )
    )

    await contributions.observers[0].handler(observation)

    assert len(seen) == 1
    assert isinstance(seen[0], sdk_type)
    payload = _worker_observation_payload(seen[0])
    assert payload["event"] == event
    assert wire_fields <= payload.keys()


@pytest.mark.parametrize("event", [item[0] for item in OBSERVATIONS])
def test_manifest_accepts_every_public_observation_type(event: str) -> None:
    contributes = PluginContributes.model_validate({"observers": [{"id": "facts", "event": event}]})

    assert contributes.observers[0].event == event


def test_manifest_rejects_unknown_observation_and_misplaced_runtime_filter() -> None:
    with pytest.raises(ValidationError, match="event"):
        PluginContributes.model_validate({"observers": [{"id": "facts", "event": "before_model"}]})
    with pytest.raises(ValidationError, match="event_types"):
        PluginContributes.model_validate(
            {
                "observers": [
                    {
                        "id": "facts",
                        "event": "context_prepared",
                        "event_types": ["run_started"],
                    }
                ]
            }
        )
    with pytest.raises(ValidationError, match="unknown runtime event types"):
        PluginContributes.model_validate(
            {
                "observers": [
                    {
                        "id": "facts",
                        "event": "runtime_event",
                        "event_types": ["not_a_public_event"],
                    }
                ]
            }
        )


@pytest.mark.asyncio
async def test_inprocess_and_worker_observers_reject_return_values() -> None:
    contributions = adapt_sdk_contributions(
        owner_id="example.plugin",
        contributions=sdk.ContributionSet(
            observers=[
                sdk.ObserverContribution(
                    id="invalid",
                    event="context_prepared",
                    handler=lambda _observation: {"control": "not-allowed"},
                )
            ]
        )
    )

    with pytest.raises(TypeError, match="must return None"):
        await contributions.observers[0].handler(OBSERVATIONS[1][1])
    with pytest.raises(WorkerProtocolError, match="must be empty"):
        _validate_worker_result("observer.observe", {"control": "not-allowed"})


@pytest.mark.asyncio
async def test_sync_sdk_observer_does_not_block_the_event_loop() -> None:
    entered = threading.Event()
    release = threading.Event()
    handler_threads: list[int] = []

    def blocking_handler(_observation: sdk.Observation) -> None:
        handler_threads.append(threading.get_ident())
        entered.set()
        release.wait()

    contributions = adapt_sdk_contributions(
        owner_id="example.plugin",
        contributions=sdk.ContributionSet(
            observers=[
                sdk.ObserverContribution(
                    id="blocking",
                    event="context_prepared",
                    handler=blocking_handler,
                )
            ]
        )
    )
    watchdog = threading.Timer(1, release.set)
    watchdog.start()
    event_loop_thread = threading.get_ident()
    observation = asyncio.create_task(contributions.observers[0].handler(OBSERVATIONS[1][1]))

    try:
        handler_entered = await asyncio.to_thread(entered.wait, 2)
        event_loop_remained_responsive = not observation.done()
    finally:
        release.set()
        watchdog.cancel()
    await observation

    assert handler_entered is True
    assert event_loop_remained_responsive is True
    assert handler_threads != [event_loop_thread]
