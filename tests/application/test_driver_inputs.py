import asyncio
import json

import pytest

from ouro.application.plugins.commands import consume_plugin_command, submit_plugin_command
from ouro.config.queued_inputs import QueuedInputConfig
from ouro.core.prompt_policies import (
    PromptPolicyDecision,
    PromptPolicyRegistration,
    PromptPolicyRegistry,
)
from ouro.core.provider import ProviderStreamEvent, ToolCallDelta
from ouro.core.session_controls import HostSessionInput
from ouro.sdk.plugins import v5 as sdk
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from tests.application.helpers import wait_for_runtime_event
from tests.application.test_run_drivers import DRIVER, PLUGIN, host, plugin_source, set_driver_body
from tests.application.test_runtime import GateProvider


class SteeringProvider:
    provider_id = "steering"

    def __init__(self, *, tool=False):
        self.started, self.release = asyncio.Event(), asyncio.Event()
        self.requests = []
        self.tool = tool

    async def stream(self, *, messages, **kwargs):
        self.requests.append([message.text_content for message in messages])
        if len(self.requests) == 1:
            self.started.set()
            await self.release.wait()
        if self.tool and len(self.requests) == 1:
            yield ProviderStreamEvent(
                type="tool_call_delta",
                tool_call_delta=ToolCallDelta(
                    index=0, id_delta="deploy", name_delta="Deploy", arguments_delta="{}"
                ),
            )
        else:
            yield ProviderStreamEvent(type="text_delta", text="done")
        yield ProviderStreamEvent(type="done")


@pytest.mark.parametrize("kind", ["python-inprocess", "worker"])
@pytest.mark.parametrize("store_type", [LocalSessionStore, SQLiteSessionStore])
@pytest.mark.parametrize("explicit", [False, True])
async def test_custom_driver_delivers_steering_at_the_next_decision_in_the_same_run(
    tmp_path, kind, store_type, explicit
):
    store = store_type(tmp_path / "sessions")
    store.initialize()
    runtime, plugins, service = host(tmp_path, store, [])
    provider = SteeringProvider()
    runtime.adapter.provider = provider
    package = plugin_source(tmp_path, kind)
    if explicit:
        set_driver_body(
            package,
            """async def review(context, resume):
    await context.models.request(tools=())
    inputs = await context.inputs.consume()
    assert await context.inputs.consume() == ()
    assert inputs[0].role == "user" and not inputs[0].scaffold
    assert inputs[0].metadata["session_input_source"] == "user"
    text = "|".join(item.text for item in inputs)
    inputs[0].content[0]["text"] = "must not mutate native history"
    inputs[0].metadata.clear()
    await context.models.request(tools=())
    return sdk.DriverFinished(text)
""",
        )
    service.install(package)
    running = None
    try:
        assert (await service.enable(PLUGIN)).status == "active"
        session = runtime.create_session()
        running = asyncio.create_task(
            runtime.run("initial", session_id=session.session_id, driver_id=DRIVER)
        )
        await wait_for_runtime_event(provider.started, running, description="driver model request")
        receipt = await runtime.run(
            "steering-marker", session_id=session.session_id, queue_mode="steer"
        )
        assert receipt.run.status == "running"
        provider.release.set()
        result = await asyncio.wait_for(running, 15)
        assert result.run.status == "completed", result.run.error
        assert len(store.load_runs(session.session_id)) == 1
        assert [any("steering-marker" in text for text in row) for row in provider.requests] == [
            False,
            True,
        ]
        if explicit:
            assert result.final_text == "steering-marker"
        history = store.load_runtime_session(session.session_id)
        delivered = [
            message
            for message in history.messages
            if message.role == "user" and message.text_content == "steering-marker"
        ]
        assert len(delivered) == 1 and delivered[0].run_id == result.run.run_id
        assert not runtime.queued_inputs.eligible_session(history.session)
    finally:
        provider.release.set()
        if running is not None:
            await asyncio.gather(running, return_exceptions=True)
        await runtime.close()
        await plugins.close()


@pytest.mark.parametrize("kind", ["python-inprocess", "worker"])
@pytest.mark.parametrize("mode", ["one_by_one", "all_at_once"])
@pytest.mark.parametrize("repeat", [False, True])
async def test_explicit_input_boundary_preserves_queue_units_and_follow_up(
    tmp_path, kind, mode, repeat
):
    store = LocalSessionStore(tmp_path / "sessions")
    store.initialize()
    runtime, plugins, service = host(tmp_path, store, [])
    runtime.queued_input_config = QueuedInputConfig(consumption_mode=mode)
    provider = SteeringProvider()
    runtime.adapter.provider = provider
    package = plugin_source(tmp_path, kind)
    set_driver_body(
        package,
        """async def review(context, resume):
    await context.models.request(tools=())
    first = await context.inputs.consume()
    if REPEAT:
        first += await context.inputs.consume()
    await context.models.request(tools=())
    await context.models.request(tools=())
    return sdk.DriverFinished("|".join(item.text for item in first))
""".replace("REPEAT", repr(repeat)),
    )
    service.install(package)
    running = None
    try:
        assert (await service.enable(PLUGIN)).status == "active"
        session = runtime.create_session()
        running = asyncio.create_task(
            runtime.run("initial", session_id=session.session_id, driver_id=DRIVER)
        )
        await wait_for_runtime_event(provider.started, running, description="driver model request")
        for prompt, queue_mode in (
            ("steer-one", "steer"),
            ("steer-two", "steer"),
            ("later", "follow_up"),
        ):
            await runtime.run(prompt, session_id=session.session_id, queue_mode=queue_mode)
        provider.release.set()
        await running
        requests = provider.requests
        assert len(requests) == 4
        assert "steer-one" in requests[1]
        assert ("steer-two" in requests[1]) == (repeat or mode == "all_at_once")
        assert "steer-two" in requests[2]
        assert all("later" not in row for row in requests[:3])
        assert "later" in requests[3]
        runs = store.load_runs(session.session_id)
        assert [run.driver.driver_id for run in runs] == [DRIVER, "ouro.default"]
        assert all(run.status == "completed" for run in runs)
    finally:
        provider.release.set()
        if running is not None:
            await asyncio.gather(running, return_exceptions=True)
        await runtime.close()
        await plugins.close()


@pytest.mark.parametrize("kind", ["python-inprocess", "worker"])
@pytest.mark.parametrize("ending", ["policy", "cancel"])
async def test_undelivered_steering_is_retained_on_policy_rejection_or_cancellation(
    tmp_path, kind, ending
):
    store = LocalSessionStore(tmp_path / "sessions")
    store.initialize()
    runtime, plugins, service = host(tmp_path, store, [])
    provider = SteeringProvider()
    runtime.adapter.provider = provider
    denied = False

    def guard(call):
        if denied and call.prompt == "steering-marker":
            return PromptPolicyDecision(action="deny", reason="denied at delivery")
        return PromptPolicyDecision()

    policies = PromptPolicyRegistry()
    policies.register(PromptPolicyRegistration("ouro.host", "guard", guard))
    plugins.coordinator.prompt_policies = policies
    service.install(plugin_source(tmp_path, kind))
    running = None
    try:
        assert (await service.enable(PLUGIN)).status == "active"
        session = runtime.create_session()
        running = asyncio.create_task(
            runtime.run("initial", session_id=session.session_id, driver_id=DRIVER)
        )
        await wait_for_runtime_event(provider.started, running, description="driver model request")
        await runtime.run("steering-marker", session_id=session.session_id, queue_mode="steer")
        accepted = runtime.queued_inputs.eligible_session(
            store.load_session_record(session.session_id)
        )
        assert len(accepted) == 1
        if ending == "cancel":
            running.cancel()
            with pytest.raises(asyncio.CancelledError):
                await running
        else:
            denied = True
            provider.release.set()
            result = await running
            assert result.run.status == "failed"
            assert "denied at delivery" in result.run.error
        history = store.load_runtime_session(session.session_id)
        assert len(history.runs) == 1
        assert len(provider.requests) == 1
        assert runtime.queued_inputs.eligible_session(history.session) == accepted
        assert all(message.text_content != "steering-marker" for message in history.messages)
    finally:
        provider.release.set()
        if running is not None:
            await asyncio.gather(running, return_exceptions=True)
        await runtime.close()
        await plugins.close()


@pytest.mark.parametrize("kind", ["python-inprocess", "worker"])
@pytest.mark.parametrize("store_type", [LocalSessionStore, SQLiteSessionStore])
async def test_steering_waits_for_model_tool_results_and_survives_driver_restart(
    tmp_path, kind, store_type
):
    store = store_type(tmp_path / "sessions")
    store.initialize()
    calls = []
    runtime, plugins, service = host(tmp_path, store, calls)
    provider = SteeringProvider(tool=True)
    runtime.adapter.provider = provider
    package = plugin_source(tmp_path, kind)
    set_driver_body(
        package,
        """async def review(context, resume):
    if resume is None:
        response = await context.models.request()
        try:
            await context.inputs.consume()
        except (ValueError, RuntimeError) as error:
            assert "pending model tool response" in str(error)
        else:
            raise AssertionError("input split a model tool response")
        result = await context.tools.execute_model_response(
            response.response_id, checkpoint="resume"
        )
        if result.suspended:
            return sdk.DriverSuspended()
    inputs = await context.inputs.consume()
    return sdk.DriverFinished("|".join(item.text for item in inputs))
""",
    )
    service.install(package)
    running = None
    try:
        assert (await service.enable(PLUGIN)).status == "active"
        session = runtime.create_session()
        running = asyncio.create_task(
            runtime.run("initial", session_id=session.session_id, driver_id=DRIVER)
        )
        await wait_for_runtime_event(provider.started, running, description="driver model request")
        await runtime.run("steering-marker", session_id=session.session_id, queue_mode="steer")
        provider.release.set()
        waiting = await running
        assert waiting.run.status == "waiting_approval", waiting.run.error
        history = store.load_runtime_session(session.session_id)
        assert [message.text_content for message in history.messages if message.role == "user"] == [
            "initial"
        ]
        assert len(runtime.queued_inputs.eligible_session(history.session)) == 1
        await runtime.close()
        await plugins.close()
        runtime, plugins, service = host(tmp_path, store, calls)
        await plugins.start()
        result = await runtime.approve_tool(
            session_id=session.session_id,
            run_id=waiting.run.run_id,
            approval_id=waiting.run.pending_approval_id,
            approved=True,
        )
        assert result.run.status == "completed", result.run.error
        assert result.final_text == "steering-marker"
        history = store.load_runtime_session(session.session_id)
        assert len(history.runs) == 1 and calls == ["deploy"]
        roles = [message.role for message in history.messages]
        assert roles == ["user", "assistant", "tool_result", "user", "assistant"]
        assert not runtime.queued_inputs.eligible_session(history.session)
    finally:
        provider.release.set()
        if running is not None:
            await asyncio.gather(running, return_exceptions=True)
        await runtime.close()
        await plugins.close()


async def test_busy_session_keeps_the_selected_driver_for_queued_follow_up(tmp_path):
    store = LocalSessionStore(tmp_path / "sessions")
    store.initialize()
    runtime, plugins, service = host(tmp_path, store, [])
    provider = GateProvider()
    runtime.adapter.provider = provider
    package = plugin_source(tmp_path, "python-inprocess")
    set_driver_body(
        package, 'async def review(context, resume):\n    return sdk.DriverFinished("chosen")\n'
    )
    service.install(package)
    running = None
    try:
        assert (await service.enable(PLUGIN)).status == "active"
        session = runtime.create_session(title="queued algorithms")
        running = asyncio.create_task(runtime.run("first", session_id=session.session_id))
        await wait_for_runtime_event(
            provider.first_started, running, description="model request before queued follow-up"
        )
        await runtime.run("next", session_id=session.session_id, driver_id=DRIVER)
        provider.release_first.set()
        await running
        runs = store.load_runs(session.session_id)
        assert [item.driver.driver_id for item in runs] == ["ouro.default", DRIVER]
        assert provider.calls == 1
        messages = store.load_runtime_session(session.session_id).messages
        assert messages[-1].text_content == "chosen"
    finally:
        provider.release_first.set()
        if running is not None:
            await asyncio.gather(running, return_exceptions=True)
        await runtime.close()
        await plugins.close()


async def test_all_at_once_keeps_queued_command_release_bindings_separate_after_restart(tmp_path):
    store = LocalSessionStore(tmp_path / "sessions")
    store.initialize()
    runtime, plugins, service = host(tmp_path, store, [])
    package = plugin_source(tmp_path, "python-inprocess")
    set_driver_body(
        package, 'async def review(context, resume):\n    return sdk.DriverFinished("old")\n'
    )
    installed = service.install(package)
    try:
        assert (await service.enable(PLUGIN)).status == "active"
        session = runtime.create_session()
        original = await submit_plugin_command(
            service, runtime, "review", "first", session_id=session.session_id
        )
        assert original is not None
        accepted = runtime.queued_inputs.eligible_session(
            store.load_session_record(session.session_id)
        )
        assert accepted[0].driver.implementation.release_id == installed.package_digest
        set_driver_body(
            package, 'async def review(context, resume):\n    return sdk.DriverFinished("new")\n'
        )
        manifest_path = package / ".ouro-plugin/plugin.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["version"] = "0.1.1"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        replacement = service.install(package)
        assert (await service.select(PLUGIN, replacement.package_digest)).status == "active"
        second = await submit_plugin_command(
            service, runtime, "review", "second", session_id=session.session_id
        )
        assert second is not None
        await plugins.wait_stopped(PLUGIN)
        removed = await service.remove(PLUGIN, package_digest=installed.package_digest)
        assert removed.retained_by == [f"{session.session_id}/queued"]
        assert not service.gc()
        service.install(plugins.store.package_path(PLUGIN, installed.package_digest))
        await runtime.close()
        await plugins.close()
        runtime, plugins, service = host(tmp_path, store, [])
        runtime.queued_input_config = QueuedInputConfig(consumption_mode="all_at_once")
        await plugins.start()
        results = [item async for item in consume_plugin_command(runtime, second)]
        assert results[-1].run.status == "completed", results[-1].run.error
        runs = store.load_runs(session.session_id)
        assert [item.driver.implementation.release_id for item in runs] == [
            installed.package_digest,
            replacement.package_digest,
        ]
        assert all(item.status == "completed" for item in runs)
        messages = store.load_runtime_session(session.session_id).messages
        assert [item.text_content for item in messages if item.role == "assistant"] == [
            "old",
            "new",
        ]
        assert not runtime.queued_inputs.bound_drivers(
            store.load_session_record(session.session_id)
        )
        assert (await service.remove(PLUGIN, package_digest=installed.package_digest)).changed
    finally:
        await runtime.close()
        await plugins.close()


@pytest.mark.parametrize("request_type", [sdk.SessionInput, HostSessionInput])
def test_driver_selection_requires_follow_up_input(request_type):
    extra = {"host_source_id": "test"} if request_type is HostSessionInput else {}
    with pytest.raises(ValueError, match="follow-up"):
        request_type(prompt="request", mode="steer", driver_id=DRIVER, **extra)


def test_atomic_sdk_input_batch_requires_one_driver_selection():
    with pytest.raises(ValueError, match="same Driver"):
        sdk.SessionInputBatch(
            inputs=(
                sdk.SessionInput(prompt="review", driver_id=DRIVER),
                sdk.SessionInput(prompt="default"),
            )
        )


@pytest.mark.parametrize("requires_session", [True, False])
@pytest.mark.parametrize("role", [None, "system"])
async def test_worker_command_atomic_batch_keeps_driver_selection_and_input_order(
    tmp_path, requires_session, role
):
    store = SQLiteSessionStore(tmp_path / "sessions.sqlite3")
    store.initialize()
    runtime, plugins, service = host(tmp_path, store, [])
    package = plugin_source(tmp_path, "worker")
    manifest_path = package / ".ouro-plugin/plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["contributes"]["commands"][0]["requires_session"] = requires_session
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    source = package / "review_driver_plugin.py"
    text = source.read_text(encoding="utf-8")
    start, end = text.index("def request_review("), text.index("async def review(")
    text = (
        text[:start]
        + """def request_review(call):
    return sdk.CommandOutput(session_input_batches=(sdk.SessionInputBatch(inputs=(
        sdk.SessionInput(prompt="criteria", driver_id="DRIVER"ROLE),
        sdk.SessionInput(prompt=call.argument, role="user", driver_id="DRIVER"),
    )),))


""".replace("DRIVER", DRIVER).replace("ROLE", f", role={role!r}" if role else "")
        + text[end:]
    )
    source.write_text(text, encoding="utf-8")
    set_driver_body(
        package, 'async def review(context, resume):\n    return sdk.DriverFinished("batch done")\n'
    )
    service.install(package)
    try:
        assert (await service.enable(PLUGIN)).status == "active"
        submission = await submit_plugin_command(
            service, runtime, "review", "request", session_id=None
        )
        assert submission is not None
        results = [item async for item in consume_plugin_command(runtime, submission)]
        assert len(results) == 1
        assert results[0].run.driver.driver_id == DRIVER
        assert results[0].final_text == "batch done"
        history = store.load_runtime_session(submission.session_id)
        assert len(history.runs) == 1
        assert [item.text_content for item in history.messages] == [
            "criteria",
            "request",
            "batch done",
        ]
        assert [item.role for item in history.messages] == [role or "user", "user", "assistant"]
        assert {item.metadata["input_driver_id"] for item in history.messages[:2]} == {DRIVER}
        assert len({item.run_id for item in history.messages}) == 1
    finally:
        await runtime.close()
        await plugins.close()


@pytest.mark.parametrize(
    ("store_type", "kind"),
    [(LocalSessionStore, "python-inprocess"), (SQLiteSessionStore, "worker")],
)
async def test_sdk_review_command_uses_native_approval_and_resumes_after_restart(
    tmp_path, store_type, kind
):
    store = store_type(tmp_path / ("sessions.sqlite3" if kind == "worker" else "sessions"))
    store.initialize()
    calls = []
    runtime, plugins, service = host(tmp_path, store, calls)
    service.install(plugin_source(tmp_path, kind))
    try:
        assert (await service.enable(PLUGIN)).status == "active"
        submitted = await submit_plugin_command(
            service, runtime, "review", "deploy", session_id=None
        )
        assert submitted is not None and submitted.has_inputs
        results = [item async for item in consume_plugin_command(runtime, submitted)]
        assert len(results) == 1
        pending = results[0]
        assert pending.run.status == "waiting_approval", pending.run.error
        assert pending.run.driver.driver_id == DRIVER
        assert pending.run.driver_checkpoint.payload == {"step": "summarize", "review": "reviewed"}
        assert pending.run.model_round_count == 2
        assert calls == []
        await runtime.close()
        await plugins.close()
        runtime, plugins, service = host(tmp_path, store, calls)
        await plugins.start()
        result = await runtime.approve_tool(
            session_id=pending.session.session_id,
            run_id=pending.run.run_id,
            approval_id=pending.run.pending_approval_id,
            approved=True,
        )
        assert result.run.status == "completed", result.run.error
        assert result.final_text == "deployed"
        assert result.run.driver == pending.run.driver
        assert result.run.driver_checkpoint.state == "consumed"
        assert calls == ["deploy"]
    finally:
        await runtime.close()
        await plugins.close()


@pytest.mark.parametrize("consume", [False, True])
async def test_host_input_retry_keeps_receipt_when_selected_driver_is_disabled(tmp_path, consume):
    store = LocalSessionStore(tmp_path / "sessions")
    store.initialize()
    runtime, plugins, service = host(tmp_path, store, [])
    package = plugin_source(tmp_path, "python-inprocess")
    set_driver_body(
        package, 'async def review(context, resume):\n    return sdk.DriverFinished("done")\n'
    )
    service.install(package)
    try:
        assert (await service.enable(PLUGIN)).status == "active"
        session = runtime.create_session()
        request = HostSessionInput(
            prompt="review", host_source_id="test", queue_id="once", driver_id=DRIVER
        )
        accepted = await runtime.submit_host_input(request, session_id=session.session_id)
        if consume:
            result = await runtime.consume_host_input(accepted)
            assert result.run.status == "completed", result.run.error
        await service.disable(PLUGIN)
        retried = await runtime.submit_host_input(request, session_id=session.session_id)
        assert retried.queue_id == accepted.queue_id
        queued = runtime.queued_inputs.eligible_session(
            store.load_session_record(session.session_id)
        )
        assert len(queued) == (0 if consume else 1)
        with pytest.raises(ValueError, match="another input"):
            await runtime.submit_host_input(
                request.model_copy(update={"driver_id": None}), session_id=session.session_id
            )
    finally:
        await runtime.close()
        await plugins.close()
