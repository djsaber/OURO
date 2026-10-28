"""Bidirectional JSON-RPC transport and worker process ownership."""

from __future__ import annotations

import asyncio
import json
import math
import os
import sys
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from contextvars import Context, ContextVar, copy_context
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ouro.core.callbacks import CallbackCancelled
from ouro.core.cancellation import settle_owned_tasks
from ouro.core.redaction import SensitiveValueRedactor
from ouro.local_processes import LocalProcess, start_process
from ouro.plugins.paths import portable_relative_path
from ouro.plugins.worker.codec import WorkerProtocolError

WorkerRequestHandler = Callable[[str, Mapping[str, object]], Awaitable[object]]


_CURRENT_REQUESTS: ContextVar[tuple[tuple[object, int | str], ...]] = ContextVar(
    "ouro_worker_host_requests", default=()
)


@dataclass
class _IncomingRequest:
    task: asyncio.Task[None]
    parent: int | None
    nested: bool
    executing: bool = True


class JsonRpcWorkerClient:
    def __init__(
        self,
        command: list[str],
        *,
        cwd: Path,
        diagnostic: Callable[[str, str], None],
        environment: Mapping[str, str] | None = None,
        redaction_values: tuple[str, ...] = (),
        request_timeout: float = 30.0,
        max_message_bytes: int = 4 * 1024 * 1024,
        request_handler: WorkerRequestHandler | None = None,
        max_incoming_requests: int = 32,
        max_pending_requests: int = 128,
    ) -> None:
        if min(max_incoming_requests, max_pending_requests) < 1:
            raise ValueError("worker request concurrency must be positive")
        self.command = _resolve_worker_command(command, cwd)
        self.cwd = cwd
        self.diagnostic = diagnostic
        self.environment = dict(environment or {})
        self.redactor = SensitiveValueRedactor(redaction_values)
        self.request_timeout = request_timeout
        self.max_message_bytes = max_message_bytes
        self.request_handler = request_handler
        self.max_incoming_requests = max_incoming_requests
        self.max_pending_requests = max_pending_requests
        self.process: asyncio.subprocess.Process | None = None
        self._process_owner: LocalProcess | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[object]] = {}
        self._pending_contexts: dict[int, Context] = {}
        self._incoming: dict[int | str, _IncomingRequest] = {}
        self._request_id = 0
        self._write_lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None

    @property
    def is_healthy(self) -> bool:
        process = self.process
        return (
            not self._closed
            and process is not None
            and process.returncode is None
            and self._reader_task is not None
            and not self._reader_task.done()
        )

    async def start(self) -> None:
        async with self._start_lock:
            await self._start_locked()

    async def _start_locked(self) -> None:
        if self.process is not None:
            return
        if self._closed:
            raise WorkerProtocolError("plugin worker is closed")
        environment = {
            key: value
            for key, value in os.environ.items()
            if key.upper() in {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"}
        }
        environment.update(self.environment)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        self._process_owner = await start_process(
            *self.command,
            cwd=self.cwd,
            env=environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=self.max_message_bytes + 1,
        )
        self.process = self._process_owner.process
        self._reader_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._read_stderr())

    async def request(
        self,
        method: str,
        params: Mapping[str, object] | None = None,
        *,
        timeout: float | None = None,
        retain_result_on_cancel: bool = False,
    ) -> object:
        await self.start()
        if self._closed and method != "shutdown":
            raise WorkerProtocolError("plugin worker is closed")
        process = self.process
        assert process is not None and process.stdin is not None
        if process.returncode is not None:
            raise WorkerProtocolError(f"plugin worker exited with code {process.returncode}")
        if len(self._pending) >= self.max_pending_requests:
            raise WorkerProtocolError("plugin worker request capacity is exhausted")
        self._request_id += 1
        request_id = self._request_id
        future: asyncio.Future[object] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        self._pending_contexts[request_id] = copy_context()
        request_timeout = timeout if timeout is not None else self.request_timeout
        sent = False

        def mark_sent() -> None:
            nonlocal sent
            sent = True

        try:
            message: dict[str, object] = {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": dict(params or {}),
                "timeout_seconds": request_timeout,
            }
            for client, identity in reversed(_CURRENT_REQUESTS.get()):
                if client is self:
                    message["parent_id"] = identity
                    break
            await self._write(message, on_sent=mark_sent)
            response = asyncio.shield(future) if retain_result_on_cancel else future
            return await asyncio.wait_for(response, timeout=request_timeout or None)
        except TimeoutError:
            with suppress(Exception):
                await self.notify("cancel", {"id": request_id})
            raise
        except asyncio.CancelledError:
            if not sent:
                future.cancel()
                raise
            # The response may still describe an actual external effect, but the
            # request can no longer authorize reverse calls or staged state.
            self._pending_contexts.pop(request_id, None)
            cancellation = asyncio.create_task(self.notify("cancel", {"id": request_id}))
            await self._cancel_children(request_id)
            await settle_owned_tasks(cancellation)
            with suppress(Exception):
                cancellation.result()
            if retain_result_on_cancel:
                await settle_owned_tasks(future)
                if not future.cancelled():
                    try:
                        result = future.result()
                    except Exception:
                        # A transport error is not a returned tool observation.
                        pass
                    else:
                        raise CallbackCancelled(result) from None
            raise
        finally:
            self._pending.pop(request_id, None)
            self._pending_contexts.pop(request_id, None)
            if not future.done():
                future.cancel()
            await self._cancel_children(request_id)

    async def _cancel_children(self, request_id: int) -> None:
        children = [item.task for item in self._incoming.values() if item.parent == request_id]
        for task in children:
            task.cancel()
        if children:
            await settle_owned_tasks(*children)
            for task in children:
                if not task.cancelled():
                    task.exception()

    async def notify(self, method: str, params: Mapping[str, object] | None = None) -> None:
        await self.start()
        await self._write({"jsonrpc": "2.0", "method": method, "params": dict(params or {})})

    async def close(self, *, timeout: float = 5.0) -> None:
        if self._close_task is not None and self._close_task.done():
            if self._close_task.cancelled() or self._close_task.exception() is not None:
                self._close_task = None
        if self._close_task is None:
            self._closed = True
            self._close_task = asyncio.create_task(self._close(timeout=timeout))
        await asyncio.shield(self._close_task)

    async def _close(self, *, timeout: float) -> None:
        async with self._start_lock:
            process = self.process
        if process is None:
            return
        if os.name != "nt" and process.returncode is None:
            with suppress(Exception):
                await self.request("shutdown", timeout=min(timeout, 2.0))
        assert self._process_owner is not None
        await self._process_owner.close()
        callbacks = [item.task for item in self._incoming.values()]
        for task in callbacks:
            task.cancel()
        if callbacks:
            await asyncio.gather(*callbacks, return_exceptions=True)
        if process.stdin is not None:
            process.stdin.close()
            with suppress(BrokenPipeError, ConnectionError):
                await process.stdin.wait_closed()

        # A subprocess transport is not closed until the process and every pipe
        # have finished. Protocol readers can stop before EOF after invalid
        # output; drain any remaining bytes to resume paused pipe transports.
        await asyncio.gather(
            *(task for task in (self._reader_task, self._stderr_task) if task is not None),
            return_exceptions=True,
        )
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                while await stream.read(64 * 1024):
                    pass
        await process.wait()
        self.process = None
        self._process_owner = None
        self._reader_task = None
        self._stderr_task = None
        error = WorkerProtocolError("plugin worker closed")
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self._pending.clear()

    async def _write(
        self, message: dict[str, object], *, on_sent: Callable[[], None] | None = None
    ) -> None:
        process = self.process
        if process is None or process.stdin is None:
            raise WorkerProtocolError("plugin worker is not running")
        encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > self.max_message_bytes:
            raise WorkerProtocolError("plugin worker request exceeds message limit")
        async with self._write_lock:
            process.stdin.write(encoded + b"\n")
            if on_sent is not None:
                on_sent()
            await process.stdin.drain()

    async def _read_stdout(self) -> None:
        process = self.process
        assert process is not None and process.stdout is not None
        try:
            while True:
                try:
                    line = await process.stdout.readline()
                except ValueError as exc:
                    raise WorkerProtocolError(
                        "plugin worker response exceeds message limit"
                    ) from exc
                if not line:
                    break
                if len(line) > self.max_message_bytes:
                    raise WorkerProtocolError("plugin worker response exceeds message limit")
                try:
                    message = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise WorkerProtocolError("plugin worker wrote invalid JSON to stdout") from exc
                if not isinstance(message, dict):
                    raise WorkerProtocolError("plugin worker response must be an object")
                if message.get("jsonrpc") != "2.0":
                    raise WorkerProtocolError("plugin worker response has invalid JSON-RPC version")
                request_id = message.get("id")
                method = message.get("method")
                if isinstance(method, str) and request_id is not None:
                    await self._dispatch_request(message)
                    continue
                if method == "cancel":
                    params = message.get("params")
                    if not isinstance(params, dict) or type(params.get("id")) not in (int, str):
                        raise WorkerProtocolError("worker cancellation requires a request id")
                    incoming = self._incoming.get(params["id"])
                    if incoming is not None:
                        incoming.task.cancel()
                    continue
                if type(request_id) is int:
                    has_result = "result" in message
                    has_error = "error" in message
                    if has_result == has_error:
                        raise WorkerProtocolError(
                            "plugin worker response must contain exactly one of result or error"
                        )
                    future = self._pending.get(request_id)
                    if future is None or future.done():
                        continue
                    error = message.get("error")
                    if has_error:
                        if not isinstance(error, dict):
                            raise WorkerProtocolError(
                                "plugin worker response error must be an object"
                            )
                        future.set_exception(
                            WorkerProtocolError(
                                "plugin worker request failed: " + self._redact(str(error))
                            )
                        )
                    else:
                        future.set_result(message.get("result"))
                    continue
                if message.get("method") == "diagnostic.emit":
                    params = message.get("params", {})
                    if not isinstance(params, dict):
                        raise WorkerProtocolError(
                            "plugin worker diagnostic params must be an object"
                        )
                    self._emit_diagnostic(
                        str(params.get("code", "worker_diagnostic")),
                        str(params.get("message", "")),
                    )
                    continue
                raise WorkerProtocolError("plugin worker wrote an unknown message to stdout")
            if not self._closed:
                code = process.returncode
                raise WorkerProtocolError(
                    f"plugin worker stdout closed unexpectedly (exit code {code})"
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - fail all outstanding RPC calls.
            error = WorkerProtocolError(self._redact(str(exc)))
            if not self._closed:
                self._emit_diagnostic("worker_exit", str(error))
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(error)

    async def _dispatch_request(self, message: dict[str, Any]) -> None:
        request_id = message["id"]
        if type(request_id) not in (int, str):
            raise WorkerProtocolError("worker request id must be an integer or string")
        if request_id in self._incoming:
            raise WorkerProtocolError("worker reused an outstanding request id")
        params = message.get("params", {})
        parent = message.get("parent_id")
        timeout = message.get("timeout_seconds", self.request_timeout)
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout < 0:
            await self._reply_error(
                request_id, -32602, "request timeout must be finite and nonnegative"
            )
            return
        if not isinstance(params, dict) or (
            parent is not None and (type(parent) is not int or parent not in self._pending_contexts)
        ):
            await self._reply_error(request_id, -32602, "invalid request parameters or parent")
            return
        if self._closed:
            await self._reply_error(request_id, -32000, "plugin worker is closing")
            return
        if self.request_handler is None:
            await self._reply_error(request_id, -32601, "Host requests are unavailable")
            return
        context = self._pending_contexts[parent].copy() if parent is not None else Context()
        ancestors = context.get(_CURRENT_REQUESTS, ())
        nested = any(client is self for client, _ in ancestors)
        if len(self._incoming) >= self.max_pending_requests or (
            not nested
            and sum(not item.nested and item.executing for item in self._incoming.values())
            >= self.max_incoming_requests
        ):
            # Never wait for a business slot in the reader. A waiting Host
            # request still needs this reader to receive its worker response.
            await self._reply_error(request_id, -32001, "Host request capacity is exhausted")
            return
        task = asyncio.create_task(
            self._serve_request(request_id, message["method"], params, parent, timeout),
            name=f"ouro-worker-request-{request_id}",
            context=context,
        )
        self._incoming[request_id] = _IncomingRequest(task, parent, nested)
        task.add_done_callback(self._request_finished)

    def _request_finished(self, task: asyncio.Task[None]) -> None:
        if not task.cancelled() and (error := task.exception()) is not None:
            self._emit_diagnostic("worker_host_request_failed", str(error))

    async def _serve_request(
        self,
        request_id: int | str,
        method: str,
        params: Mapping[str, object],
        parent: int | None,
        timeout: float,
    ) -> None:
        token = _CURRENT_REQUESTS.set((*_CURRENT_REQUESTS.get(), (self, request_id)))
        try:
            assert self.request_handler is not None
            try:
                async with asyncio.timeout(timeout or None):
                    result = await self.request_handler(method, params)
                self._incoming[request_id].executing = False
                if not self._closed and (parent is None or parent in self._pending):
                    await self._write({"jsonrpc": "2.0", "id": request_id, "result": result})
            except asyncio.CancelledError:
                if parent is None or parent in self._pending:
                    await self._reply_error(request_id, -32800, "Host request cancelled")
                raise
            except Exception as exc:
                await self._reply_error(request_id, -32000, self._redact(str(exc)))
        finally:
            _CURRENT_REQUESTS.reset(token)
            self._incoming.pop(request_id, None)

    async def _reply_error(self, request_id: int | str, code: int, message: str) -> None:
        if self._closed:
            return
        if incoming := self._incoming.get(request_id):
            incoming.executing = False
        try:
            await self._write(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": code, "message": _bounded_text(message, limit=4096)},
                }
            )
        except BrokenPipeError, ConnectionError:
            # The peer may exit while its accepted requests are being settled.
            return

    async def _read_stderr(self) -> None:
        process = self.process
        assert process is not None and process.stderr is not None
        emitted = 0
        while chunk := await process.stderr.read(4096):
            if emitted >= 100:
                continue
            emitted += 1
            self._emit_diagnostic(
                "worker_stderr",
                chunk.decode("utf-8", errors="replace").rstrip(),
            )

    def _redact(self, value: str) -> str:
        return self.redactor.redact(value)

    def report_invalid_result(self, error: WorkerProtocolError) -> None:
        self._emit_diagnostic("worker_invalid_result", str(error))

    def _emit_diagnostic(self, code: str, message: str) -> None:
        bounded_code = _bounded_text(code, limit=128, fallback="worker_diagnostic")
        bounded_message = _bounded_text(self._redact(message), limit=4096)
        with suppress(Exception):
            self.diagnostic(bounded_code, bounded_message)


def _bounded_text(value: str, *, limit: int, fallback: str = "") -> str:
    normalized = value.strip() or fallback
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1] + "…"


def _resolve_worker_command(command: list[str], package_root: Path) -> list[str]:
    resolved: list[str] = []
    for index, item in enumerate(command):
        if item == "{python}":
            resolved.append(sys.executable)
            continue
        if item.startswith("./"):
            try:
                relative = portable_relative_path(item[2:])
            except ValueError as exc:
                raise WorkerProtocolError(f"worker command path is invalid: {item}: {exc}") from exc
            path = package_root.joinpath(*relative.parts).resolve()
            if not path.is_relative_to(package_root.resolve()) or not path.is_file():
                raise WorkerProtocolError(f"worker command path is outside package: {item}")
            resolved.append(str(path))
            continue
        if index == 0:
            raise WorkerProtocolError(
                "worker executable must be '{python}' or a package-relative './' path"
            )
        resolved.append(item)
    return resolved
