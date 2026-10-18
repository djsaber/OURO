import asyncio
from collections import Counter
from unittest.mock import patch

import pytest

from ouro.application.plugins import PluginService
from ouro.core.observers import ObserverRegistry
from ouro.plugins.builtin import BuiltinPluginDefinition, BuiltinPluginRegistry
from ouro.plugins.contributions import ContributionCoordinator
from ouro.plugins.dependencies import BuiltinPluginContract, plan_desired_plugin_activation
from ouro.plugins.manifest import PluginManifest
from ouro.plugins.models import BuiltinPluginActivationOverride, PluginStateDocument
from ouro.plugins.registry import PluginCommandRegistry
from ouro.plugins.runtime import PluginRuntimeResource
from ouro.plugins.store import PluginPackageStore
from ouro.sdk.plugins import v4 as sdk
from ouro.skills import SkillMetadata
from ouro.tools import ToolRegistry
from tests.plugins.test_plugin_system import _runtime, _write_native_plugin


def graph_runtime(tmp_path, dependencies, *, fail=None, phase="prepare", on_prepare=None):
    events = []

    class Plugin:
        def __init__(self, context):
            self.key = context.identity.plugin_id.rsplit(".", 1)[-1]
            events.append(("create", self.key))

        async def prepare(self):
            if on_prepare is not None:
                await on_prepare(self.key)
            if self.key == fail and phase == "prepare":
                raise ValueError("prepare failed")

            async def start():
                events.append(("service_start", self.key))

            async def stop():
                events.append(("service_stop", self.key))

            return sdk.ContributionSet(
                background_services=[sdk.BackgroundServiceContribution("watch", start, stop)]
            )

        async def start(self):
            if self.key == fail and phase == "start":
                raise ValueError("start failed")

        async def stop(self, reason, deadline):
            events.append(("stop", self.key))

    definitions = []
    for key, required in dependencies.items():
        definitions.append(
            BuiltinPluginDefinition(
                manifest=PluginManifest.model_validate(
                    {
                        "schema_version": 3,
                        "plugin_id": f"test.graph.{key}",
                        "name": key,
                        "namespace": key,
                        "version": "1.0.0",
                        "engines": {"ouro": "^0.3.0", "plugin_api": "^4.0.0"},
                        "runtime": {
                            "kind": "python-inprocess",
                            "entrypoint": f"{key}_plugin:create",
                        },
                        "dependencies": {
                            "plugins": [
                                {"plugin_id": f"test.graph.{item}", "version": "^1.0.0"}
                                for item in required
                            ]
                        },
                        "contributes": {"background_services": [{"id": "watch"}]},
                    }
                ),
                factory=Plugin,
                root=tmp_path,
            )
        )
    runtime = PluginRuntimeResource(
        PluginPackageStore(tmp_path / "home"),
        ContributionCoordinator(
            tools=ToolRegistry(), observers=ObserverRegistry(), commands=PluginCommandRegistry()
        ),
        builtin_plugins=BuiltinPluginRegistry(definitions),
    )
    return runtime, events


@pytest.mark.parametrize(
    "dependencies",
    [
        {"a": [], "b": ["a"], "c": ["b"], "d": ["c"]},
        {"a": [], "b": ["a"], "c": ["a"], "d": ["b", "c"]},
        {"a": [], "b": [], "c": [], "d": []},
    ],
    ids=["chain", "diamond", "independent"],
)
async def test_start_builds_and_starts_each_required_generation_once(tmp_path, dependencies):
    runtime, events = graph_runtime(tmp_path, dependencies)
    try:
        with patch.object(runtime.store, "read_state", wraps=runtime.store.read_state) as reads:
            await runtime.start()
            # One recovery snapshot and two optimistic commit checks, independent of graph size.
            assert reads.call_count == 3
        assert Counter(key for action, key in events if action == "create") == Counter(
            dependencies.keys()
        )
        assert [key for action, key in events if action == "service_start"] == list(dependencies)
        assert not any(action in {"stop", "service_stop"} for action, _ in events)
        assert len(runtime.catalog().generations) == len(dependencies)
        before = list(events)
        await runtime.start()
        await runtime.synchronize()
        assert events == before
    finally:
        await runtime.close()
    assert Counter(key for action, key in events if action == "service_stop") == Counter(
        dependencies.keys()
    )


@pytest.mark.parametrize("phase", ["prepare", "start"])
async def test_start_keeps_healthy_candidates_when_one_dependency_fails(tmp_path, phase):
    runtime, events = graph_runtime(
        tmp_path,
        {"a": [], "b": [], "c": ["b"], "d": ["a"]},
        fail="b",
        phase=phase,
    )
    try:
        await runtime.start()
        assert Counter(key for action, key in events if action == "create") == Counter("abd")
        assert [key for action, key in events if action == "service_start"] == ["a", "d"]
        assert {item.plugin_id for item in runtime.catalog().generations} == {
            "test.graph.a",
            "test.graph.d",
        }
        assert runtime.status("test.graph.b") == "failed"
        assert runtime.status("test.graph.c") == "blocked"
    finally:
        await runtime.close()


async def test_start_discards_staged_graph_if_shared_state_changes(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()

    async def prepare(key):
        if key == "b":
            entered.set()
            await release.wait()

    runtime, events = graph_runtime(tmp_path, {"a": [], "b": ["a"]}, on_prepare=prepare)
    starting = asyncio.create_task(runtime.start())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        state = runtime.store.read_state()
        updated = state.model_copy(deep=True)
        updated.builtin_activation_overrides["test.graph.b"] = BuiltinPluginActivationOverride(
            desired_enabled=False
        )
        runtime.store.commit_candidate_state(state, updated)
        release.set()
        await starting
        assert runtime.catalog().generations == ()
        assert not any(action == "service_start" for action, _ in events)
        assert Counter(key for action, key in events if action == "stop") == Counter("ab")
        await runtime.synchronize()
        assert [item.plugin_id for item in runtime.catalog().generations] == ["test.graph.a"]
    finally:
        release.set()
        await runtime.close()


async def test_catalog_conflict_discards_only_the_failed_candidate_and_its_dependants(tmp_path):
    runtime, *_ = _runtime(
        tmp_path / "home",
        base_skills=(SkillMetadata(name="calendar", description="Host", location="host/SKILL.md"),),
    )
    service = PluginService(runtime.store, runtime=runtime)
    for key, dependencies in {"a": [], "b": [], "c": ["b"], "d": ["a"]}.items():
        source = tmp_path / key
        _write_native_plugin(
            source,
            plugin_id=f"test.graph.{key}",
            dependencies=[
                {"plugin_id": f"test.graph.{item}", "version": "^1.0.0"} for item in dependencies
            ],
            contributes={"skills": [{"id": "calendar", "path": "./skills/calendar"}]}
            if key == "b"
            else {},
        )
        if key == "b":
            skill = source / "skills/calendar/SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("---\nname: calendar\ndescription: Conflict\n---\n", encoding="utf-8")
        service.install(source)
    state = runtime.store.read_state()
    enabled = state.model_copy(deep=True)
    for record in enabled.plugins.values():
        record.activation.desired_enabled = True
    runtime.store.commit_candidate_state(state, enabled)
    try:
        with patch.object(runtime, "_build_generation", wraps=runtime._build_generation) as builds:
            await runtime.start()
        assert builds.call_count == 4
        assert {item.plugin_id for item in runtime.catalog().generations} == {
            "test.graph.a",
            "test.graph.d",
        }
        assert runtime.catalog().skills[0].description == "Host"
        assert runtime.status("test.graph.b") == "failed"
        assert runtime.status("test.graph.c") == "blocked"
        assert not runtime.has_resources("test.graph.b")
        assert not runtime.has_resources("test.graph.c")
    finally:
        await runtime.close()


def test_startup_dependency_plan_isolates_cycles_and_edge_version_failures():
    dependencies = {
        "a": [("b", "^1.0.0")],
        "b": [("a", "^1.0.0")],
        "c": [("d", "^2.0.0")],
        "d": [],
        "e": [("d", "^1.0.0")],
    }
    contracts = {
        key: BuiltinPluginContract(key, "1.0.0", key, tuple(required))
        for key, required in dependencies.items()
    }
    plan = plan_desired_plugin_activation(PluginStateDocument(), builtin_contracts=contracts)
    assert plan.graph.activation_order == ("d", "e")
    assert {key for key, _ in plan.failures} == {"a", "b", "c"}
