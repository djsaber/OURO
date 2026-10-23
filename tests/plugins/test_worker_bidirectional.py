from __future__ import annotations

import asyncio
import textwrap
from contextvars import ContextVar

import pytest

import ouro.plugins.worker.client as worker_client
from ouro.plugins.worker.client import JsonRpcWorkerClient


@pytest.fixture
def worker_program(tmp_path):
    path = tmp_path / "peer.py"
    path.write_text(
        textwrap.dedent("""
        import asyncio
        import json
        import sys

        pending = {}
        calls = {}

        def send(message):
            print(json.dumps({'jsonrpc': '2.0', **message}), flush=True)

        async def handle(message):
            key = message['id']
            if message['method'] == 'inner':
                send({'id': key, 'result': message['params']})
                return
            callback_id = 'reverse-' + str(key)
            pending[callback_id] = asyncio.get_running_loop().create_future()
            send({'id': callback_id, 'method': 'host.call',
                  'parent_id': key, 'params': message.get('params', {})})
            try:
                result = await pending[callback_id]
                send({'id': key, 'result': result})
            except asyncio.CancelledError:
                send({'method': 'cancel', 'params': {'id': callback_id}})
            finally:
                pending.pop(callback_id, None)
                calls.pop(key, None)

        async def main():
            while line := await asyncio.to_thread(sys.stdin.readline):
                message = json.loads(line)
                if message.get('method') == 'shutdown':
                    send({'id': message['id'], 'result': None})
                    return
                if message.get('method') == 'cancel':
                    task = calls.get(message['params']['id'])
                    if task:
                        task.cancel()
                elif 'method' in message:
                    calls[message['id']] = asyncio.create_task(handle(message))
                else:
                    future = pending.get(message['id'])
                    if future and not future.done():
                        future.set_result(message.get('result', message.get('error')))

        asyncio.run(main())
    """),
        encoding="utf-8",
    )
    return path


async def test_reverse_call_preserves_parent_context_and_can_call_worker_at_capacity_one(
    worker_program,
):
    authority = ContextVar("authority", default="unbound")

    async def host(method, params):
        assert method == "host.call"
        assert authority.get() == "accepted-call"
        return await client.request("inner", params)

    client = JsonRpcWorkerClient(
        ["{python}", worker_program.name],
        cwd=worker_program.parent,
        diagnostic=lambda *_: None,
        request_handler=host,
        max_incoming_requests=1,
    )
    try:
        await client.start()
        token = authority.set("accepted-call")
        try:
            assert await client.request("outer", {"value": "round trip"}) == {"value": "round trip"}
        finally:
            authority.reset(token)
    finally:
        await client.close()


async def test_parent_cancellation_settles_accepted_host_calls(worker_program):
    entered = asyncio.Event()
    stopped = asyncio.Event()

    async def host(method, params):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    client = JsonRpcWorkerClient(
        ["{python}", worker_program.name],
        cwd=worker_program.parent,
        diagnostic=lambda *_: None,
        request_handler=host,
    )
    try:
        request = asyncio.create_task(client.request("outer"))
        async with asyncio.timeout(10):
            await entered.wait()
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
            await stopped.wait()
        assert not client._incoming
        assert not client._pending
    finally:
        await client.close()


async def test_saturated_reverse_calls_fail_without_blocking_responses(worker_program):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def host(method, params):
        entered.set()
        await release.wait()
        return "settled"

    client = JsonRpcWorkerClient(
        ["{python}", worker_program.name],
        cwd=worker_program.parent,
        diagnostic=lambda *_: None,
        request_handler=host,
        max_incoming_requests=1,
    )
    first = asyncio.create_task(client.request("outer"))
    try:
        async with asyncio.timeout(10):
            await entered.wait()
            busy = await client.request("outer")
            assert busy["code"] == -32001
            release.set()
            assert await first == "settled"
    finally:
        release.set()
        await asyncio.gather(first, return_exceptions=True)
        await client.close()


async def test_concurrent_worker_start_has_one_process_owner(worker_program, monkeypatch):
    owners = []
    launch = worker_client.start_process

    async def observed_launch(*args, **kwargs):
        owner = await launch(*args, **kwargs)
        owners.append(owner)
        return owner

    monkeypatch.setattr(worker_client, "start_process", observed_launch)
    client = JsonRpcWorkerClient(
        ["{python}", worker_program.name], cwd=worker_program.parent, diagnostic=lambda *_: None
    )
    try:
        await asyncio.gather(*(client.start() for _ in range(4)))
        assert len(owners) == 1
    finally:
        await client.close()
        for owner in owners:
            await owner.close()


async def test_worker_cleanup_failure_retains_owner_for_explicit_retry(worker_program, monkeypatch):
    client = JsonRpcWorkerClient(
        ["{python}", worker_program.name], cwd=worker_program.parent, diagnostic=lambda *_: None
    )
    await client.start()
    owner = client._process_owner
    assert owner is not None
    close = owner.close
    attempts = 0

    async def fail_once():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("process cleanup temporarily unavailable")
        await close()

    monkeypatch.setattr(owner, "close", fail_once)
    try:
        with pytest.raises(OSError, match="cleanup temporarily"):
            await client.close()
        assert client._process_owner is owner
        assert owner.process.returncode is None
        await client.close()
        assert owner.process.returncode is not None
        assert client.process is None
    finally:
        await close()


async def test_worker_close_waits_for_a_start_already_owning_a_process(worker_program, monkeypatch):
    launched, release = asyncio.Event(), asyncio.Event()
    owners = []
    launch = worker_client.start_process

    async def held_launch(*args, **kwargs):
        owner = await launch(*args, **kwargs)
        owners.append(owner)
        launched.set()
        await release.wait()
        return owner

    monkeypatch.setattr(worker_client, "start_process", held_launch)
    client = JsonRpcWorkerClient(
        ["{python}", worker_program.name], cwd=worker_program.parent, diagnostic=lambda *_: None
    )
    starting = asyncio.create_task(client.start())
    closing = None
    try:
        async with asyncio.timeout(10):
            await launched.wait()
            closing = asyncio.create_task(client.close())
            await asyncio.sleep(0)
            assert not closing.done()
            release.set()
            await starting
            await closing
        assert owners[0].process.returncode is not None
        assert client.process is None
    finally:
        release.set()
        await asyncio.gather(starting, return_exceptions=True)
        if closing is not None:
            await asyncio.gather(closing, return_exceptions=True)
        await client.close()
