from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import ouro.local_processes.commands as local_commands
from ouro.local_application import open_local_plugins
from ouro.plugins.invocation import plugin_invocation
from ouro.plugins.registry import PluginCommandError

EXAMPLES = Path(__file__).parents[2] / "examples" / "plugins"
MEMORY = "dev.ouro.examples.memory"
CLIENT = "dev.ouro.examples.memory_client"


async def test_worker_services_reenter_both_workers_at_capacity_one_and_release_filters(tmp_path):
    async with open_local_plugins(tmp_path / "home") as plugins:
        plugins.install(EXAMPLES / "memory-service")
        plugins.install(EXAMPLES / "memory-client")
        activation = await plugins.enable(CLIENT)
        assert activation.status == "active", plugins.show(CLIENT).model_dump()
        runtime = plugins.runtime
        consumer = runtime._active[CLIENT]
        provider = runtime._active[MEMORY]
        for generation in (consumer, provider):
            generation.instance.client.max_incoming_requests = 1

        async with asyncio.timeout(15):
            assert (await plugins.execute_command(f"{CLIENT}:remember", "OURO")).output == "Stored."
            found = await plugins.execute_command(f"{CLIENT}:search", "OU")
            assert found.output == "Memory: OURO"
        assert await provider.services.call(MEMORY, "memory", "search", "OU") == ["OURO"]
        assert not consumer.services.registrations
        assert not provider.services.registrations
        assert not consumer.instance.service_bridge._registrations
        assert not provider.instance.service_bridge._handlers

        with plugin_invocation(CLIENT, writable=False):
            with pytest.raises(PermissionError, match="read-only"):
                await consumer.services.call(MEMORY, "memory", "append", "forbidden")
        assert (await plugins.disable(CLIENT)).status == "disabled"
        assert not runtime.has_resources(CLIENT)
        assert (await plugins.disable(MEMORY)).status == "disabled"
        assert not runtime.has_resources(MEMORY)

    async with open_local_plugins(tmp_path / "home") as plugins:
        assert (await plugins.enable(CLIENT)).status == "active"
        assert (await plugins.execute_command(f"{CLIENT}:search", "OU")).output == "Memory: OURO"


async def test_worker_exec_uses_host_owned_process_and_streams_back_into_busy_worker(tmp_path):
    async with open_local_plugins(tmp_path / "home") as plugins:
        plugins.install(EXAMPLES / "worker-exec")
        identity = "dev.ouro.examples.worker_exec"
        activation = await plugins.enable(identity)
        assert activation.status == "active", plugins.show(identity).model_dump()
        result = await plugins.execute_command(f"{identity}:python", "print(6 * 7)")
        assert result.output.strip() == "42"
        assert result.data["returncode"] == 0
        assert result.data["output_chunks"] >= 1
        assert (await plugins.disable(identity)).status == "disabled"
        assert not plugins.runtime.has_resources(identity)


async def test_disabling_worker_command_settles_its_reverse_host_process(tmp_path, monkeypatch):
    owners = []
    start_process = local_commands.start_process

    async def start(*args, **kwargs):
        owner = await start_process(*args, **kwargs)
        owners.append(owner)
        return owner

    monkeypatch.setattr(local_commands, "start_process", start)
    async with open_local_plugins(tmp_path / "home") as plugins:
        plugins.install(EXAMPLES / "worker-exec")
        identity = "dev.ouro.examples.worker_exec"
        assert (await plugins.enable(identity)).status == "active"
        ready = tmp_path / "ready"
        code = (
            f"import pathlib,threading;pathlib.Path({str(ready)!r}).touch();"
            "threading.Event().wait()"
        )
        command = asyncio.create_task(plugins.execute_command(f"{identity}:python", code))
        try:
            async with asyncio.timeout(15):
                while not ready.exists():
                    await asyncio.sleep(0.01)
                assert (await plugins.disable(identity)).status == "disabled"
                with pytest.raises(PluginCommandError):
                    await command
            assert not plugins.runtime.has_resources(identity)
            assert len(owners) == 1 and owners[0].process.returncode is not None
        finally:
            command.cancel()
            await asyncio.gather(command, return_exceptions=True)
            for owner in owners:
                await owner.close()


async def test_retiring_worker_crash_releases_resources_despite_an_old_catalog_lease(tmp_path):
    async with open_local_plugins(tmp_path / "home") as plugins:
        plugins.install(EXAMPLES / "worker-tool")
        identity = "dev.ouro.examples.worker_tool"
        assert (await plugins.enable(identity)).status == "active"
        runtime = plugins.runtime
        old = runtime._active[identity]
        lease = runtime.snapshot()
        try:
            assert (await plugins.restart(identity)).status == "active"
            old.instance.client.process.kill()
            async with asyncio.timeout(5):
                while not old.stopped:
                    await asyncio.sleep(0.01)
            assert old.instance.client.process is None
            assert runtime.status(identity) == "active"
            assert lease.catalog_id != runtime.catalog().catalog_id
        finally:
            lease.close()
