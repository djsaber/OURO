from __future__ import annotations

import asyncio
import json
import shutil
import sys
import textwrap
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

from ouro.application.plugins.service import PluginService
from ouro.application.tasks.scheduler import TaskScheduler
from tests.application.test_tasks import task_runtime
from tests.plugins.test_plugin_system import _runtime, _write_native_plugin

PLUGIN_ID = "dev.ouro.home_service"


@asynccontextmanager
async def worker_host(home):
    runtime, *_ = _runtime(home)
    scheduler = None
    try:
        await runtime.start()
        async with task_runtime(home) as (tasks, execution, _, _):
            elected = asyncio.Event()
            loop = asyncio.get_running_loop()
            acquire = tasks.repository.acquire_scheduler

            def observed_election():
                result = acquire()
                loop.call_soon_threadsafe(elected.set)
                return result

            scheduler = TaskScheduler(
                tasks,
                execution,
                refresh_seconds=0.05,
                refresh_environment=runtime.synchronize,
                owned_services=runtime,
            )
            with patch.object(tasks.repository, "acquire_scheduler", observed_election):
                await scheduler.start()
                try:
                    async with asyncio.timeout(15):
                        await elected.wait()
                    yield runtime, scheduler
                finally:
                    await scheduler.close()
    finally:
        await runtime.close()


async def test_worker_home_service_handover_between_real_hosts_and_disable(tmp_path):
    home = tmp_path / "home"
    source = tmp_path / "package"
    _write_native_plugin(
        source,
        plugin_id=PLUGIN_ID,
        runtime={"kind": "worker", "command": ["{python}", "worker.py"]},
        contributes={"background_services": [{"id": "singleton", "scope": "home"}]},
    )
    shutil.copyfile(
        Path(__file__).parent / "fixtures" / "home_service_worker.py", source / "worker.py"
    )
    runtime, *_ = _runtime(home)
    plugins = PluginService(runtime.store, runtime=runtime)
    data = runtime.store.data_root / PLUGIN_ID
    try:
        plugins.install(source)
        assert (await plugins.enable(PLUGIN_ID)).status == "active", runtime.diagnostics(PLUGIN_ID)
        assert not (data / "owner").exists()
        assert not (data / "events.jsonl").exists()
    finally:
        await runtime.close()

    script = textwrap.dedent(
        """
        import asyncio, json, sys
        from pathlib import Path
        from tests.plugins.test_worker_home_services import PLUGIN_ID, worker_host

        async def main():
            async with worker_host(Path(sys.argv[1])) as (runtime, scheduler):
                print(json.dumps({'ready': True}), flush=True)
                while action := (await asyncio.to_thread(sys.stdin.readline)).strip():
                    if action == 'close':
                        break
                    await runtime.synchronize()
                    if action == 'stopped':
                        await runtime.wait_stopped(PLUGIN_ID)
                    print(json.dumps({
                        'status': runtime.status(PLUGIN_ID),
                        'diagnostics': [d.message for d in scheduler.diagnostics],
                    }), flush=True)
        asyncio.run(main())
        """
    )

    def events():
        path = data / "events.jsonl"
        return (
            [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            if path.exists()
            else []
        )

    async def wait_events(count):
        async with asyncio.timeout(15):
            while len(events()) < count:
                await asyncio.sleep(0.01)
        return events()

    child = None
    try:
        async with worker_host(home):
            first = (await wait_events(1))[0]
            child = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                script,
                str(home),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            assert child.stdin is not None and child.stdout is not None

            async def read_child():
                async with asyncio.timeout(15):
                    line = await child.stdout.readline()
                assert line, await child.stderr.read()
                return json.loads(line)

            async def child_status(*, stopped=False):
                child.stdin.write(b"stopped\n" if stopped else b"status\n")
                await child.stdin.drain()
                return await read_child()

            assert await read_child() == {"ready": True}
            # Host readiness includes a real singleton election attempt.
            assert await child_status() == {"status": "active", "diagnostics": []}
            assert events() == [first]
            assert (data / "owner").read_text(encoding="utf-8") == str(first["pid"])

        transferred = await wait_events(3)
        assert [item["action"] for item in transferred] == ["start", "stop", "start"]
        assert transferred[1]["pid"] == first["pid"]
        assert transferred[2]["pid"] != first["pid"]
        assert await child_status() == {"status": "active", "diagnostics": []}
        # Save disable intent from another runtime; the owner refreshes it normally.
        controller, *_ = _runtime(home)
        try:
            assert await controller.disable(PLUGIN_ID) == "disabled"
        finally:
            await controller.close()
        stopped = await wait_events(4)
        assert stopped[-1] == {"action": "stop", "pid": transferred[2]["pid"]}
        assert not (data / "owner").exists()
        assert await child_status(stopped=True) == {"status": "disabled", "diagnostics": []}
        child.stdin.write(b"close\n")
        await child.stdin.drain()
        async with asyncio.timeout(15):
            assert await child.wait() == 0
    finally:
        if child is not None and child.returncode is None:
            child.kill()
            await child.wait()
