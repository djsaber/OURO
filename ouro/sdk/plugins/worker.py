"""Async JSON-RPC peer for Python plugin workers; stdout belongs to the protocol."""

from __future__ import annotations

import asyncio
import json
import math
import sys
from collections.abc import Awaitable, Callable, Mapping
from contextvars import ContextVar
from typing import Any

_REQUEST: ContextVar[int | str | None] = ContextVar("ouro_worker_request", default=None)
type RequestHandler = Callable[[Mapping[str, Any]], Awaitable[Any]]


class HostRequestError(RuntimeError):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


class WorkerPeer:
    """Dispatch requests independently while retaining parent calls and cancellation."""

    def __init__(
        self,
        handlers: Mapping[str, RequestHandler],
        *,
        max_requests: int = 32,
        max_pending_requests: int = 128,
        max_message_bytes: int = 4 * 1024 * 1024,
        request_timeout: float = 30,
    ) -> None:
        if min(max_requests, max_pending_requests, max_message_bytes, request_timeout) <= 0:
            raise ValueError("worker limits must be positive")
        self.handlers = dict(handlers)
        self.notifications: dict[str, Callable[[Mapping[str, Any]], None]] = {}
        self.max_requests = max_requests
        self.max_pending_requests = max_pending_requests
        self.max_message_bytes = max_message_bytes
        self.request_timeout = request_timeout
        self._next_id = 0
        self._pending: dict[str, asyncio.Future[Any]] = {}
        self._parents: dict[str, int | str | None] = {}
        self._calls: dict[int | str, asyncio.Task[None]] = {}
        self._roots: set[int | str] = set()
        self._write_lock = asyncio.Lock()
        self._closed = False

    async def request(
        self,
        method: str,
        params: Mapping[str, object] | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        if self._closed:
            raise RuntimeError("worker peer is closed")
        if len(self._pending) >= self.max_pending_requests:
            raise RuntimeError("Host request capacity is exhausted")
        parent = _REQUEST.get()
        if parent is not None and parent not in self._calls:
            raise RuntimeError("worker invocation has finished")
        self._next_id += 1
        identity = f"worker-{self._next_id}"
        future = asyncio.get_running_loop().create_future()
        self._pending[identity] = future
        self._parents[identity] = parent
        request_timeout = timeout if timeout is not None else self.request_timeout
        try:
            message: dict[str, object] = {
                "id": identity,
                "method": method,
                "params": dict(params or {}),
                "timeout_seconds": request_timeout,
            }
            if parent is not None:
                message["parent_id"] = parent
            await self._write(message)
            return await asyncio.wait_for(future, request_timeout or None)
        except asyncio.CancelledError, TimeoutError:
            await self.notify("cancel", {"id": identity})
            raise
        finally:
            self._pending.pop(identity, None)
            self._parents.pop(identity, None)

    async def notify(self, method: str, params: Mapping[str, object]) -> None:
        await self._write({"method": method, "params": dict(params)})

    async def close_registration(
        self, identity: str, *, method: str = "registrations.close"
    ) -> Any:
        # Cleanup is owner-bound and remains possible after its acquiring call ends.
        token = _REQUEST.set(None)
        try:
            return await self.request(method, {"registration_id": identity})
        finally:
            _REQUEST.reset(token)

    async def _write(self, message: Mapping[str, object]) -> None:
        encoded = json.dumps(
            {"jsonrpc": "2.0", **message}, ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
        if len(encoded) > self.max_message_bytes:
            raise ValueError("worker message exceeds message limit")
        if "result" in message or "error" in message:
            self._roots.discard(message["id"])

        def write() -> None:
            sys.stdout.buffer.write(encoded + b"\n")
            sys.stdout.buffer.flush()

        async with self._write_lock:
            task = asyncio.create_task(asyncio.to_thread(write))
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise

    async def serve(self) -> None:
        async with asyncio.TaskGroup() as tasks:
            try:
                await self._read_requests(tasks)
            finally:
                self._closed = True
                pending = tuple(self._calls.values())
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                for future in self._pending.values():
                    future.cancel()

    async def _read_requests(self, tasks: asyncio.TaskGroup) -> None:
        while line := await asyncio.to_thread(
            sys.stdin.buffer.readline, self.max_message_bytes + 1
        ):
            if len(line) > self.max_message_bytes:
                raise ValueError("Host message exceeds message limit")
            message = json.loads(line)
            if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                raise ValueError("invalid Host JSON-RPC message")
            identity = message.get("id")
            method = message.get("method")
            if method == "shutdown":
                await self._write({"id": identity, "result": None})
                return
            if method == "cancel":
                task = self._calls.get(message.get("params", {}).get("id"))
                if task is not None:
                    task.cancel()
            elif (
                isinstance(method, str)
                and isinstance(identity, (int, str))
                and not isinstance(identity, bool)
            ):
                if identity in self._calls:
                    raise ValueError("Host reused an outstanding request id")
                timeout = message.get("timeout_seconds", self.request_timeout)
                if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout < 0:
                    await self._error(
                        identity, -32602, "request timeout must be finite and nonnegative"
                    )
                    continue
                parent = message.get("parent_id")
                nested = parent is not None and parent in self._pending
                if parent is not None and not nested:
                    await self._error(identity, -32602, "parent request is unavailable")
                    continue
                if len(self._calls) >= self.max_pending_requests or (
                    not nested and len(self._roots) >= self.max_requests
                ):
                    await self._error(identity, -32001, "worker request capacity is exhausted")
                    continue
                if not nested:
                    self._roots.add(identity)
                self._calls[identity] = tasks.create_task(
                    self._serve(identity, method, message.get("params", {}), timeout)
                )
            elif type(identity) is str and (future := self._pending.get(identity)) is not None:
                if not future.done():
                    if "error" in message:
                        error = message["error"]
                        future.set_exception(HostRequestError(error["code"], error["message"]))
                    else:
                        future.set_result(message.get("result"))
            elif identity is not None:
                continue  # A cancelled request may still receive its final response.
            elif isinstance(method, str) and method in self.notifications:
                params = message.get("params", {})
                if not isinstance(params, dict):
                    raise ValueError("Host notification params must be an object")
                self.notifications[method](params)
            else:
                raise ValueError("unsupported Host notification")

    async def _serve(
        self, identity: int | str, method: str, params: object, timeout: float
    ) -> None:
        token = _REQUEST.set(identity)
        try:
            handler = self.handlers.get(method)
            if handler is None:
                await self._error(identity, -32601, f"unknown worker method: {method}")
                return
            if not isinstance(params, dict):
                await self._error(identity, -32602, "request params must be an object")
                return
            try:
                async with asyncio.timeout(timeout or None):
                    result = await handler(params)
                await self._write({"id": identity, "result": result})
            except asyncio.CancelledError:
                await self._error(identity, -32800, "worker request cancelled")
                raise
            except Exception as exc:
                await self._error(identity, -32000, f"{type(exc).__name__}: {exc}")
        finally:
            _REQUEST.reset(token)
            self._roots.discard(identity)
            self._calls.pop(identity, None)
            for key, parent in self._parents.items():
                if parent == identity:
                    self._pending[key].cancel()

    async def _error(self, identity: int | str, code: int, message: str) -> None:
        await self._write({"id": identity, "error": {"code": code, "message": message[:4096]}})
