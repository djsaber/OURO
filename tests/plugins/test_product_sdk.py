"""Exercise product behavior after installing it under an ordinary user identity."""

import json

from ouro.application.usage import UsageService
from ouro.billing import LocalUsageLedger
from ouro.builtin_plugins.goal import GOAL_PLUGIN
from ouro.builtin_plugins.plan import PLAN_PLUGIN
from ouro.local_application import LocalApplication
from tests.plugins.test_builtin_goal import _EndToEndGoalProvider
from tests.plugins.test_builtin_plan import FinalProvider, PlanBuildProvider, _runtime


def user_package(tmp_path, definition, tool_names):
    manifest = definition.manifest.model_dump(mode="json", exclude_none=True)
    name = manifest["namespace"]
    manifest.update(plugin_id=f"dev.example.{name}", namespace=f"user_{name}")
    manifest["runtime"]["entrypoint"] = f"user_{name}_plugin.{name}_plugin:create_plugin"
    for tool in manifest["contributes"]["tools"]:
        tool["exposed_name"] = tool_names[tool["exposed_name"]]
    source = tmp_path / f"user-{name}"
    package = source / f"user_{name}_plugin"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    for path in definition.root.glob("*.py"):
        if path.name in {"__init__.py", "definition.py"}:
            continue
        content = path.read_text(encoding="utf-8")
        for original, replacement in tool_names.items():
            content = content.replace(original, replacement)
        (package / path.name).write_text(content, encoding="utf-8")
    descriptor = source / ".ouro-plugin/plugin.json"
    descriptor.parent.mkdir()
    descriptor.write_text(json.dumps(manifest), encoding="utf-8")
    return source, manifest["plugin_id"]


class NamedToolsProvider:
    def __init__(self, provider, names):
        self.provider = provider
        self.provider_id = provider.provider_id
        self.names = names

    async def stream(self, **kwargs):
        async for event in self.provider.stream(**kwargs):
            delta = event.tool_call_delta
            if delta is not None and delta.name_delta in self.names:
                event = event.model_copy(
                    update={
                        "tool_call_delta": delta.model_copy(
                            update={"name_delta": self.names[delta.name_delta]}
                        )
                    }
                )
            yield event

    async def check_connection(self, *args, **kwargs):
        return await self.provider.check_connection(*args, **kwargs)


async def test_user_plan_owns_its_gate_and_resumes_confirmation_after_restart(tmp_path):
    names = {"EnterPlanMode": "UserEnterPlan", "ExitPlanMode": "UserExitPlan"}
    source, plugin_id = user_package(tmp_path, PLAN_PLUGIN, names)
    home, workspace = tmp_path / "home", tmp_path / "workspace"
    workspace.mkdir()
    provider = PlanBuildProvider()
    runtime, store = _runtime(home, workspace, NamedToolsProvider(provider, names))
    application = LocalApplication.from_runtime(runtime, home=home)
    try:
        await application.start()
        await application.plugins.disable(PLAN_PLUGIN.plugin_id)
        application.plugins.install(source)
        assert (await application.plugins.enable(plugin_id)).status == "active"
        pending = await runtime.run("Design the implementation", approval_mode="auto")
        assert pending.run.status == "waiting_user", pending.run.error
        call = pending.run.active_tool_batch.calls[-1]
        assert call.invocation.plugin_id == plugin_id
        assert call.invocation.plugin_class == "user"
        assert call.input_request.data["type"] == "plan_confirmation"
        assert provider.plan_path.is_relative_to(home / "artifacts" / plugin_id)
        assert store.load_session_record(pending.session.session_id).controls.activations
    finally:
        await application.close()
    runtime, store = _runtime(home, workspace, FinalProvider())
    application = LocalApplication.from_runtime(runtime, home=home)
    try:
        await application.start()
        result = await runtime.answer_user(
            session_id=pending.session.session_id,
            run_id=pending.run.run_id,
            answer={"action": "accept", "option": "Direct implementation"},
        )
        assert result.run.status == "completed", result.run.error
        assert not store.load_session_record(result.session.session_id).controls.activations
        assert "Implement safely" in provider.plan_path.read_text(encoding="utf-8")
        await application.plugins.disable(plugin_id)
        assert (await application.plugins.remove(plugin_id)).status == "disabled"
        assert provider.plan_path.exists()
    finally:
        await application.close()


async def test_user_goal_uses_native_continuation_controls_and_usage(tmp_path):
    names = {name: f"User{name}" for name in ("CreateGoal", "UpdateGoal", "GetGoal")}
    source, plugin_id = user_package(tmp_path, GOAL_PLUGIN, names)
    home, workspace = tmp_path / "home", tmp_path / "workspace"
    workspace.mkdir()
    provider = _EndToEndGoalProvider()
    ledger = LocalUsageLedger(home)
    ledger.initialize()
    runtime, store = _runtime(
        home, workspace, NamedToolsProvider(provider, names), usage_service=UsageService(ledger)
    )
    application = LocalApplication.from_runtime(runtime, home=home)
    try:
        await application.start()
        await application.plugins.disable(GOAL_PLUGIN.plugin_id)
        application.plugins.install(source)
        assert (await application.plugins.enable(plugin_id)).status == "active"
        session = runtime.create_session(approval_mode="auto")
        command = await application.plugins.execute_command(
            f"{plugin_id}:goal", "--token-budget 100 Ship end to end", session_id=session.session_id
        )
        assert len(command.accepted_inputs) == 1
        pending = runtime.queued_inputs.eligible_session(
            store.load_session_record(session.session_id)
        )
        result = await runtime.run(
            pending[0].prompt, session_id=session.session_id, _queued_inputs=pending
        )
        assert result.run.status == "completed", result.run.error
        status = await application.plugins.execute_command(
            f"{plugin_id}:goal", "status", session_id=session.session_id
        )
        assert provider.calls == 4
        assert status.data["goal"]["status"] == "complete"
        assert status.data["goal"]["tokens_used"] == 10
        assert not store.load_session_record(session.session_id).controls.activations
        await application.plugins.disable(plugin_id)
        assert (await application.plugins.remove(plugin_id)).status == "disabled"
        assert any(
            state.owner_id == plugin_id
            for state in store.load_session_extension_states(session.session_id)
        )
    finally:
        await application.close()
