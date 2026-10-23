from __future__ import annotations

import asyncio
import json
import sys
import textwrap
from unittest.mock import patch

from ouro.application.plugins.service import PluginService
from tests.plugins.test_lifecycle import PLUGIN_ID, LifecyclePlugin, create_runtime
from tests.plugins.test_plugin_system import _write_native_plugin


async def test_plugin_status_projection_uses_one_state_snapshot(tmp_path):
    runtime = create_runtime(tmp_path, LifecyclePlugin)
    service = PluginService(runtime.store, runtime=runtime, builtin_plugins=runtime.builtin_plugins)
    for index in range(3):
        source = tmp_path / f"source-{index}"
        _write_native_plugin(source, plugin_id=f"dev.ouro.probe{index}")
        service.install(source)
    expected = runtime.store.read_state()
    enabled = expected.model_copy(deep=True)
    for record in enabled.plugins.values():
        record.activation.desired_enabled = True
    runtime.store.commit_candidate_state(expected, enabled)
    try:
        with patch.object(runtime.store, "read_state", wraps=runtime.store.read_state) as read:
            statuses = {item.plugin_id: item.status for item in service.list()}
            assert statuses == {
                PLUGIN_ID: "disabled",
                **{f"dev.ouro.probe{index}": "ready" for index in range(3)},
            }
            assert read.call_count == 1
            read.reset_mock()
            assert runtime.status("dev.ouro.probe0") == "ready"
            assert read.call_count == 1
    finally:
        await runtime.close()


async def test_unchanged_state_does_not_read_or_rebuild_plugins(tmp_path):
    runtime = create_runtime(tmp_path, LifecyclePlugin)
    try:
        await runtime.start()
        await runtime.enable(PLUGIN_ID)
        await runtime.synchronize()
        catalog = runtime.catalog()
        with patch.object(runtime.store, "read_state", side_effect=AssertionError("state read")):
            await runtime.synchronize()
            await runtime.synchronize()
        assert runtime.catalog() is catalog
    finally:
        await runtime.close()


async def test_external_disable_revokes_active_and_retiring_generations(tmp_path):
    owner = create_runtime(tmp_path, LifecyclePlugin)
    viewer = create_runtime(tmp_path, LifecyclePlugin)
    lease = None
    try:
        await owner.enable(PLUGIN_ID)
        await viewer.start()
        lease = viewer.snapshot()
        await viewer.restart(PLUGIN_ID)
        assert viewer._retired
        await owner.disable(PLUGIN_ID)
        await viewer.synchronize()
        assert viewer.catalog().generations == ()
        assert await viewer.wait_stopped(PLUGIN_ID) == "disabled"
        assert not viewer.has_resources(PLUGIN_ID)
    finally:
        if lease is not None:
            lease.close()
        await viewer.close()
        await owner.close()


async def test_shared_intent_propagates_between_two_real_host_processes(tmp_path):
    script = textwrap.dedent(
        """
        import asyncio
        import json
        import sys
        from pathlib import Path
        from tests.plugins.test_lifecycle import PLUGIN_ID, LifecyclePlugin, create_runtime

        async def main():
            runtime = create_runtime(Path(sys.argv[1]), LifecyclePlugin)
            await runtime.start()
            try:
                print(json.dumps({'ready': True}), flush=True)
                while action := (await asyncio.to_thread(sys.stdin.readline)).strip():
                    if action == 'close':
                        break
                    await runtime.synchronize()
                    if not runtime.catalog().generations:
                        await runtime.wait_stopped(PLUGIN_ID)
                    print(json.dumps({
                        'active': [g.plugin_id for g in runtime.catalog().generations],
                        'owns_resources': runtime.has_resources(PLUGIN_ID),
                    }), flush=True)
            finally:
                await runtime.close()

        asyncio.run(main())
        """
    )
    owner = create_runtime(tmp_path, LifecyclePlugin)
    await owner.start()
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        str(tmp_path),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert process.stdin is not None and process.stdout is not None

    async def read():
        async with asyncio.timeout(15):
            line = await process.stdout.readline()
        assert line, await process.stderr.read()
        return json.loads(line)

    async def synchronize():
        process.stdin.write(b"synchronize\n")
        await process.stdin.drain()
        return await read()

    try:
        assert await read() == {"ready": True}
        assert await synchronize() == {"active": [], "owns_resources": False}
        await owner.enable(PLUGIN_ID)
        assert await synchronize() == {"active": [PLUGIN_ID], "owns_resources": True}
        await owner.disable(PLUGIN_ID)
        assert await synchronize() == {"active": [], "owns_resources": False}
        await owner.enable(PLUGIN_ID)
        assert await synchronize() == {"active": [PLUGIN_ID], "owns_resources": True}
        process.stdin.write(b"close\n")
        await process.stdin.drain()
        async with asyncio.timeout(15):
            assert await process.wait() == 0
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        await owner.close()
