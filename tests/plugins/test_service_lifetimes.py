from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest

from ouro.application.plugins import PluginService
from ouro.core.observers import ObserverRegistry
from ouro.plugins.contributions import ContributionCoordinator
from ouro.plugins.runtime import PluginRuntimeResource
from ouro.plugins.store import PluginPackageStore
from ouro.tools import ToolRegistry
from tests.plugins.fixtures.service_lifetime import BRANCH, LEAF, MEMORY, METHODS

OWNER = "test.lifetime.owner"
UNRELATED = "test.lifetime.unrelated"


@pytest.fixture(params=["python-inprocess", "worker"])
async def service_graph(tmp_path, request):
    runtime = PluginRuntimeResource(
        PluginPackageStore(tmp_path / "home"),
        ContributionCoordinator(tools=ToolRegistry(), observers=ObserverRegistry()),
    )
    service = PluginService(runtime.store, runtime=runtime)
    for identity, dependencies in (
        (LEAF, ()),
        (BRANCH, (LEAF,)),
        (MEMORY, ()),
        (OWNER, (LEAF, BRANCH, MEMORY)),
        (UNRELATED, ()),
    ):
        source = tmp_path / identity
        namespace = identity.rsplit(".", 1)[1]
        (source / ".ouro-plugin").mkdir(parents=True)
        shutil.copyfile(
            Path(__file__).parent / "fixtures/service_lifetime.py",
            source / f"{namespace}_plugin.py",
        )
        execution = {"kind": request.param, "entrypoint": f"{namespace}_plugin:create_plugin"}
        if request.param == "worker":
            execution = {
                "kind": "worker",
                "command": ["{python}", f"{namespace}_plugin.py"],
                "protocol": "3.0",
            }
        (source / ".ouro-plugin/plugin.json").write_text(
            json.dumps(
                {
                    "schema_version": 3,
                    "plugin_id": identity,
                    "namespace": identity.rsplit(".", 1)[1],
                    "name": identity,
                    "version": "1.0.0",
                    "engines": {"ouro": "^0.3.0", "plugin_api": "^3.0.0"},
                    "runtime": execution,
                    "dependencies": {
                        "plugins": [
                            {"plugin_id": item, "version": "^1.0.0"} for item in dependencies
                        ]
                    },
                    "exports": [
                        {
                            "id": "api",
                            "methods": {
                                name: {"effect": "state_changing", "registration": name == "filter"}
                                for name in METHODS
                            },
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        service.install(source)
    try:
        assert (await service.enable(OWNER)).status == "active", service.diagnostics()
        assert (await service.enable(UNRELATED)).status == "active", service.diagnostics()
        yield runtime
    finally:
        await runtime.close()


@pytest.mark.parametrize("entry", ["call", "callback"])
@pytest.mark.parametrize("outcome", ["return", "error", "cancel", "disable"])
async def test_accepted_service_keeps_exact_dependency_closure_until_settled(
    service_graph, tmp_path, entry, outcome
):
    runtime = service_graph
    old = dict(runtime._active)
    owner, memory = old[OWNER].services, old[MEMORY].services
    token = await old[LEAF].services.call(LEAF, "api", "identity")
    if entry == "callback":
        await owner.call(OWNER, "api", "install")
    ready, release = tmp_path / "ready", tmp_path / "release"
    endpoint, target, method = (
        (owner, OWNER, "read") if entry == "call" else (memory, MEMORY, "search")
    )
    pending = asyncio.create_task(
        endpoint.call(
            target,
            "api",
            method,
            {"ready": str(ready), "release": str(release), "fail": outcome == "error"},
        )
    )
    try:
        async with asyncio.timeout(10):
            while not ready.exists():
                if pending.done():
                    pending.result()
                await asyncio.sleep(0.01)
        # The shared leaf is leased once, even through a diamond. Neither a
        # registration at rest nor an unrelated generation holds a call lease.
        assert [old[item].lease_count for item in (OWNER, BRANCH, LEAF, UNRELATED)] == [1, 1, 1, 0]
        assert await runtime.restart(LEAF) == "active"
        assert all(not old[item].stopped for item in (OWNER, BRANCH, LEAF))
        assert await runtime._active[LEAF].services.call(LEAF, "api", "identity") != token
        if outcome == "disable":
            assert await runtime.disable(OWNER) == "disabled"
            with pytest.raises(RuntimeError):
                await pending
            with pytest.raises((RuntimeError, KeyError)):
                await owner.call(OWNER, "api", "read", {})
        elif outcome == "cancel":
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        else:
            release.touch()
            if outcome == "error":
                with pytest.raises((ValueError, RuntimeError), match="service failed"):
                    await pending
            else:
                assert await pending == [token, token]
        async with asyncio.timeout(10):
            await asyncio.gather(*(old[item].cleanup for item in (OWNER, BRANCH, LEAF)))
        assert all(old[item].stopped for item in (OWNER, BRANCH, LEAF))
        assert all(item.lease_count == 0 for item in (*old.values(), *runtime._active.values()))
        assert not memory.registrations
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
