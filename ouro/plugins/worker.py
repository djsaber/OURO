from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from collections.abc import Callable, Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any

from ouro.core.redaction import SensitiveValueRedactor
from ouro.plugins.manifest import PluginManifest
from ouro.plugins.paths import portable_relative_path
from ouro.sdk.plugins import v1 as sdk


class WorkerProtocolError(RuntimeError):
    pass


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
    ) -> None:
        self.command = _resolve_worker_command(command, cwd)
        self.cwd = cwd
        self.diagnostic = diagnostic
        self.environment = dict(environment or {})
        self.redactor = SensitiveValueRedactor(redaction_values)
        self.request_timeout = request_timeout
        self.max_message_bytes = max_message_bytes
        self.process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[object]] = {}
        self._request_id = 0
        self._write_lock = asyncio.Lock()
        self._closed = False

    async def start(self) -> None:
        if self.process is not None:
            return
        environment = {
            key: value
            for key, value in os.environ.items()
            if key.upper() in {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"}
        }
        environment.update(self.environment)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        self.process = await asyncio.create_subprocess_exec(
            *self.command,
            cwd=self.cwd,
            env=environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=os.name != "nt",
            limit=self.max_message_bytes + 1,
        )
        self._reader_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._read_stderr())

    async def request(
        self,
        method: str,
        params: Mapping[str, object] | None = None,
        *,
        timeout: float | None = None,
    ) -> object:
        await self.start()
        process = self.process
        assert process is not None and process.stdin is not None
        if process.returncode is not None:
            raise WorkerProtocolError(f"plugin worker exited with code {process.returncode}")
        self._request_id += 1
        request_id = self._request_id
        future: asyncio.Future[object] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._write(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": dict(params or {}),
                }
            )
            return await asyncio.wait_for(future, timeout=timeout or self.request_timeout)
        except TimeoutError:
            with suppress(Exception):
                await self.notify("cancel", {"id": request_id})
            raise
        finally:
            self._pending.pop(request_id, None)

    async def notify(self, method: str, params: Mapping[str, object] | None = None) -> None:
        await self.start()
        await self._write({"jsonrpc": "2.0", "method": method, "params": dict(params or {})})

    async def close(self, *, timeout: float = 5.0) -> None:
        if self._closed:
            return
        self._closed = True
        process = self.process
        if process is None:
            return
        if process.returncode is None:
            with suppress(Exception):
                await self.request("shutdown", timeout=min(timeout, 2.0))
        if process.stdin is not None:
            process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), timeout=timeout)
        except TimeoutError:
            process.kill()
            await process.wait()
        for task in (self._reader_task, self._stderr_task):
            if task is not None:
                task.cancel()
        await asyncio.gather(
            *(task for task in (self._reader_task, self._stderr_task) if task is not None),
            return_exceptions=True,
        )
        error = WorkerProtocolError("plugin worker closed")
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self._pending.clear()

    async def _write(self, message: dict[str, object]) -> None:
        process = self.process
        if process is None or process.stdin is None:
            raise WorkerProtocolError("plugin worker is not running")
        encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > self.max_message_bytes:
            raise WorkerProtocolError("plugin worker request exceeds message limit")
        async with self._write_lock:
            process.stdin.write(encoded + b"\n")
            await process.stdin.drain()

    async def _read_stdout(self) -> None:
        process = self.process
        assert process is not None and process.stdout is not None
        try:
            while line := await process.stdout.readline():
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
                if isinstance(request_id, int):
                    future = self._pending.get(request_id)
                    if future is None or future.done():
                        continue
                    error = message.get("error")
                    if error is not None:
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
                    if isinstance(params, dict):
                        self.diagnostic(
                            str(params.get("code", "worker_diagnostic")),
                            self._redact(str(params.get("message", ""))),
                        )
            if self._pending:
                code = process.returncode
                raise WorkerProtocolError(
                    f"plugin worker stdout closed unexpectedly (exit code {code})"
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - fail all outstanding RPC calls.
            error = WorkerProtocolError(self._redact(str(exc)))
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(error)

    async def _read_stderr(self) -> None:
        process = self.process
        assert process is not None and process.stderr is not None
        emitted = 0
        while line := await process.stderr.readline():
            if emitted >= 100:
                continue
            emitted += 1
            self.diagnostic(
                "worker_stderr",
                self._redact(line[:4096].decode("utf-8", errors="replace").rstrip()),
            )

    def _redact(self, value: str) -> str:
        return self.redactor.redact(value)


class WorkerPluginInstance:
    def __init__(
        self,
        manifest: PluginManifest,
        *,
        package_root: Path,
        package_digest: str,
        permissions: frozenset[str],
        configuration: Mapping[str, object],
        data_directory: Path,
        credentials: Mapping[str, str],
        diagnostic: Callable[[str, str], None],
    ) -> None:
        self.manifest = manifest
        self.package_digest = package_digest
        self.permissions = permissions
        self.configuration = dict(configuration)
        self.data_directory = data_directory
        self.credentials = dict(credentials)
        credential_environment = {
            slot: _credential_environment_name(manifest.plugin_id, slot)
            for slot in self.credentials
        }
        self.client = JsonRpcWorkerClient(
            manifest.runtime.command,
            cwd=package_root,
            diagnostic=diagnostic,
            environment={
                credential_environment[slot]: value for slot, value in self.credentials.items()
            },
            redaction_values=tuple(self.credentials.values()),
        )
        self.credential_environment = credential_environment
        self._initialized = False

    async def prepare(self) -> sdk.ContributionSet:
        self.data_directory.mkdir(parents=True, exist_ok=True)
        await self.client.start()
        initialization = await self.client.request(
            "initialize",
            {
                "protocol_version": self.manifest.runtime.protocol,
                "plugin_api": "1.0",
                "plugin_id": self.manifest.plugin_id,
                "package_digest": self.package_digest,
                "grants": sorted(self.permissions),
                "configuration": self.configuration,
                "data_directory": str(self.data_directory),
                "credential_environment": self.credential_environment,
                "features": [
                    "tools",
                    "hooks",
                    "commands",
                    "context",
                    "compaction",
                    "background_services",
                    "session_inputs",
                    "continuation_policies",
                    "tool_policies",
                ],
                "limits": {"max_message_bytes": self.client.max_message_bytes},
            },
        )
        result = _object_result(initialization, "initialize")
        negotiated = str(result.get("protocol_version", ""))
        if negotiated != self.manifest.runtime.protocol:
            raise WorkerProtocolError(
                "plugin worker protocol negotiation failed: "
                f"expected {self.manifest.runtime.protocol}, received {negotiated or '<missing>'}"
            )
        self._initialized = True
        raw = await self.client.request("contributions.prepare")
        available = _worker_contribution_ids(raw)
        _validate_worker_declarations(self.manifest, available)
        return sdk.ContributionSet(
            tools=[
                sdk.ToolContribution(
                    id=item.id,
                    exposed_name=item.exposed_name,
                    description=item.description,
                    permission_mode=item.permission_mode,
                    parameters_schema=_worker_schema(raw, "tools", item.id, "parameters_schema"),
                    result_schema=_worker_schema(raw, "tools", item.id, "result_schema"),
                    handler=self._tool_handler(item.id),
                )
                for item in self.manifest.contributes.tools
                if item.id in available.get("tools", set())
            ],
            hooks=[
                sdk.HookContribution(
                    id=item.id,
                    event=item.event,
                    priority=item.priority,
                    timeout_seconds=item.timeout_seconds,
                    handler=self._hook_handler(item.id),
                )
                for item in self.manifest.contributes.hooks
                if item.id in available.get("hooks", set())
            ],
            commands=[
                sdk.CommandContribution(
                    id=item.id,
                    description=item.description,
                    handler=self._command_handler(item.id),
                )
                for item in self.manifest.contributes.commands
                if item.id in available.get("commands", set())
            ],
            context_providers=[
                sdk.ContextContribution(
                    id=item.id,
                    handler=self._context_handler(item.id),
                )
                for item in self.manifest.contributes.context_providers
                if item.id in available.get("context_providers", set())
            ],
            compaction_strategies=[
                sdk.CompactionContribution(
                    id=item.id,
                    version="1",
                    handler=self._compaction_handler(item.id),
                )
                for item in self.manifest.contributes.compaction_strategies
                if item.id in available.get("compaction_strategies", set())
            ],
            background_services=[
                sdk.BackgroundServiceContribution(
                    id=item.id,
                    start=_noop_background_lifecycle,
                    stop=_noop_background_lifecycle,
                )
                for item in self.manifest.contributes.background_services
                if item.id in available.get("background_services", set())
            ],
            continuation_policies=[
                sdk.ContinuationPolicyContribution(
                    id=item.id,
                    handler=self._continuation_handler(item.id),
                )
                for item in self.manifest.contributes.continuation_policies
                if item.id in available.get("continuation_policies", set())
            ],
            tool_policies=[
                sdk.ToolPolicyContribution(
                    id=item.id,
                    handler=self._tool_policy_handler(item.id),
                )
                for item in self.manifest.contributes.tool_policies
                if item.id in available.get("tool_policies", set())
            ],
        )

    async def start(self) -> None:
        if self._initialized:
            await self.client.request("lifecycle.start")

    async def stop(self, reason: sdk.StopReason, deadline: float) -> None:
        del deadline
        if self._initialized:
            with suppress(Exception):
                await self.client.request("lifecycle.stop", {"reason": reason}, timeout=2.0)
        await self.client.close()

    def _tool_handler(self, identity: str):
        async def handler(call: sdk.ToolCall) -> sdk.ToolOutput:
            raw = await self.client.request(
                "tool.call",
                {
                    "contribution_id": identity,
                    "call": {
                        "tool_call_id": call.tool_call_id,
                        "session_id": call.session_id,
                        "run_id": call.run_id,
                        "tool_name": call.tool_name,
                        "arguments": dict(call.arguments),
                        "workspace": call.workspace,
                        "workspace_only": call.workspace_only,
                    },
                },
            )
            result = _object_result(raw, "tool.call")
            return sdk.ToolOutput(
                content=str(result.get("content", "")),
                structured=_object_value(result.get("structured", {})),
                resource_refs=tuple(str(item) for item in result.get("resource_refs", [])),
                is_error=bool(result.get("is_error", False)),
            )

        return handler

    def _hook_handler(self, identity: str):
        async def handler(call: sdk.HookCall) -> sdk.HookOutput:
            raw = await self.client.request(
                "hook.call",
                {
                    "contribution_id": identity,
                    "call": {
                        "event": call.event,
                        "session_id": call.session_id,
                        "run_id": call.run_id,
                        "data": dict(call.data),
                        "run_outcome": (
                            {
                                "session_id": call.run_outcome.session_id,
                                "run_id": call.run_outcome.run_id,
                                "status": call.run_outcome.status,
                                "origin": call.run_outcome.origin,
                                "final_text": call.run_outcome.final_text,
                                "error": call.run_outcome.error,
                                "usage": dict(call.run_outcome.usage),
                            }
                            if call.run_outcome is not None
                            else None
                        ),
                    },
                },
            )
            result = _object_result(raw, "hook.call")
            return sdk.HookOutput(
                added_context=tuple(str(item) for item in result.get("added_context", [])),
                diagnostics=tuple(str(item) for item in result.get("diagnostics", [])),
            )

        return handler

    def _context_handler(self, identity: str):
        async def handler(call: sdk.HookCall) -> sdk.HookOutput:
            raw = await self.client.request(
                "hook.call",
                {
                    "contribution_id": identity,
                    "call": {
                        "event": call.event,
                        "session_id": call.session_id,
                        "run_id": call.run_id,
                        "data": dict(call.data),
                        "run_outcome": (
                            {
                                "session_id": call.run_outcome.session_id,
                                "run_id": call.run_outcome.run_id,
                                "status": call.run_outcome.status,
                                "origin": call.run_outcome.origin,
                                "final_text": call.run_outcome.final_text,
                                "error": call.run_outcome.error,
                                "usage": dict(call.run_outcome.usage),
                            }
                            if call.run_outcome is not None
                            else None
                        ),
                    },
                },
            )
            result = _object_result(raw, "hook.call")
            return sdk.HookOutput(
                added_context=tuple(str(item) for item in result.get("added_context", [])),
                diagnostics=tuple(str(item) for item in result.get("diagnostics", [])),
            )

        return handler

    def _command_handler(self, identity: str):
        async def handler(call: sdk.CommandCall) -> sdk.CommandOutput:
            raw = await self.client.request(
                "command.call",
                {
                    "contribution_id": identity,
                    "call": {
                        "command_id": call.command_id,
                        "argument": call.argument,
                        "session_id": call.session_id,
                        "workspace": call.workspace,
                    },
                },
            )
            result = _object_result(raw, "command.call")
            return sdk.CommandOutput(
                output=str(result.get("output", "")),
                data=_object_value(result.get("data", {})),
                session_inputs=_worker_session_inputs(result),
            )

        return handler

    def _compaction_handler(self, identity: str):
        async def handler(call: sdk.CompactionCall) -> sdk.CompactionOutput:
            raw = await self.client.request(
                "compaction.call",
                {
                    "contribution_id": identity,
                    "call": {
                        "session_id": call.session_id,
                        "run_id": call.run_id,
                        "previous_summary": call.previous_summary,
                        "messages": [dict(message) for message in call.messages],
                        "input_token_budget": call.input_token_budget,
                        "summary_output_tokens": call.summary_output_tokens,
                    },
                },
            )
            result = _object_result(raw, "compaction.call")
            usage = {
                str(key): value
                for key, value in _object_value(result.get("usage", {})).items()
                if isinstance(value, (int, float))
            }
            return sdk.CompactionOutput(summary=str(result.get("summary", "")), usage=usage)

        return handler

    def _continuation_handler(self, identity: str):
        async def handler(call: sdk.RunOutcome) -> sdk.ContinuationDecision:
            raw = await self.client.request(
                "continuation.call",
                {
                    "contribution_id": identity,
                    "call": {
                        "session_id": call.session_id,
                        "run_id": call.run_id,
                        "status": call.status,
                        "origin": call.origin,
                        "final_text": call.final_text,
                        "error": call.error,
                        "usage": dict(call.usage),
                    },
                },
            )
            result = _object_result(raw, "continuation.call")
            action = result.get("action", "ignore")
            if action not in {"ignore", "continue", "complete", "pause", "fail"}:
                raise WorkerProtocolError(f"worker continuation action is invalid: {action}")
            mode = result.get("mode", "follow_up")
            if mode not in {"follow_up", "steer"}:
                raise WorkerProtocolError(f"worker continuation mode is invalid: {mode}")
            revision = result.get("state_revision")
            if revision is not None and not isinstance(revision, int):
                raise WorkerProtocolError("worker continuation state_revision must be an integer")
            return sdk.ContinuationDecision(
                action=action,
                prompt=(str(result["prompt"]) if result.get("prompt") is not None else None),
                mode=mode,
                title=(str(result["title"]) if result.get("title") is not None else None),
                reason=(str(result["reason"]) if result.get("reason") is not None else None),
                state_revision=revision,
            )

        return handler

    def _tool_policy_handler(self, identity: str):
        async def handler(call: sdk.ToolPolicyCall) -> sdk.ToolPolicyDecision:
            raw = await self.client.request(
                "tool_policy.call",
                {
                    "contribution_id": identity,
                    "call": {
                        "session_id": call.session_id,
                        "run_id": call.run_id,
                        "tool_call_id": call.tool_call_id,
                        "tool_name": call.tool_name,
                        "arguments": dict(call.arguments),
                        "source": call.source,
                        "workspace": call.workspace,
                    },
                },
            )
            result = _object_result(raw, "tool_policy.call")
            action = result.get("action", "ignore")
            if action not in {"ignore", "require_approval", "deny"}:
                raise WorkerProtocolError(f"worker tool policy action is invalid: {action}")
            reason = result.get("reason")
            if reason is not None and not isinstance(reason, str):
                raise WorkerProtocolError("worker tool policy reason must be a string")
            return sdk.ToolPolicyDecision(action=action, reason=reason)

        return handler


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


def _worker_contribution_ids(raw: object) -> dict[str, set[str]]:
    root = _object_result(raw, "contributions.prepare")
    result: dict[str, set[str]] = {}
    for kind, values in root.items():
        identities: set[str] = set()
        if isinstance(values, list):
            identities = {str(item) for item in values}
        elif isinstance(values, dict):
            identities = {str(item) for item in values}
        result[str(kind)] = identities
    return result


def _validate_worker_declarations(
    manifest: PluginManifest,
    available: dict[str, set[str]],
) -> None:
    for kind, declarations in manifest.contributes.by_kind().items():
        if kind in {"skills", "mcp_servers"}:
            continue
        actual = available.get(kind, set())
        declared = {item.id for item in declarations}
        extra = actual - declared
        missing = {item.id for item in declarations if not item.optional and item.id not in actual}
        if extra or missing:
            raise WorkerProtocolError(
                f"worker contributions do not match manifest for {kind}: "
                f"extra={sorted(extra)}, missing={sorted(missing)}"
            )


def _worker_schema(raw: object, kind: str, identity: str, field: str) -> dict[str, object]:
    root = _object_result(raw, "contributions.prepare")
    values = root.get(kind, {})
    if not isinstance(values, dict):
        return {}
    descriptor = values.get(identity, {})
    if not isinstance(descriptor, dict):
        return {}
    return _object_value(descriptor.get(field, {}))


def _object_result(raw: object, method: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise WorkerProtocolError(f"worker {method} result must be an object")
    return raw


def _object_value(raw: object) -> dict[str, object]:
    if not isinstance(raw, dict):
        return {}
    return {str(key): value for key, value in raw.items()}


def _worker_session_inputs(raw: dict[str, Any]) -> tuple[sdk.SessionInput, ...]:
    values = raw.get("session_inputs", [])
    if not isinstance(values, list):
        raise WorkerProtocolError("worker session_inputs result must be a list")
    inputs: list[sdk.SessionInput] = []
    for value in values:
        if not isinstance(value, dict):
            raise WorkerProtocolError("worker session input must be an object")
        prompt = value.get("prompt")
        mode = value.get("mode", "follow_up")
        title = value.get("title")
        if not isinstance(prompt, str) or not prompt.strip():
            raise WorkerProtocolError("worker session input prompt must not be empty")
        if mode not in {"follow_up", "steer"}:
            raise WorkerProtocolError(f"worker session input mode is invalid: {mode}")
        if title is not None and not isinstance(title, str):
            raise WorkerProtocolError("worker session input title must be a string")
        inputs.append(sdk.SessionInput(prompt=prompt, mode=mode, title=title))
    return tuple(inputs)


async def _noop_background_lifecycle() -> None:
    # Worker background resources are owned by lifecycle.start/stop.  These
    # descriptors provide stable identities to the host contribution contract.
    return None


def _credential_environment_name(plugin_id: str, slot: str) -> str:
    digest = hashlib.sha256(f"{plugin_id}\0{slot}".encode()).hexdigest()[:20].upper()
    return f"OURO_PLUGIN_CREDENTIAL_{digest}"
