from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import subprocess
import sys
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ouro.core.message_semantics import validate_semantic_tag
from ouro.core.redaction import SensitiveValueRedactor
from ouro.plugins.manifest import PluginManifest
from ouro.plugins.package import PLUGIN_API_VERSION
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
        except asyncio.CancelledError:
            with suppress(Exception):
                await asyncio.shield(self.notify("cancel", {"id": request_id}))
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
        if os.name != "nt" and process.returncode is None:
            with suppress(Exception):
                await self.request("shutdown", timeout=min(timeout, 2.0))
        await _terminate_process_tree(process, timeout=timeout)
        if process.stdin is not None:
            process.stdin.close()
            with suppress(BrokenPipeError, ConnectionError):
                await process.stdin.wait_closed()

        # A subprocess transport is not closed until the process and every pipe
        # have finished.  Once the whole process tree is gone, stdout/stderr
        # must reach EOF; consuming that EOF keeps ownership explicit and avoids
        # leaving platform transports to object finalizers.
        await asyncio.gather(
            *(task for task in (self._reader_task, self._stderr_task) if task is not None),
            return_exceptions=True,
        )
        await process.wait()
        self.process = None
        self._reader_task = None
        self._stderr_task = None
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

    @property
    def is_healthy(self) -> bool:
        return self._initialized and self.client.is_healthy

    async def prepare(self) -> sdk.ContributionSet:
        from ouro.plugins.contribution_types import contribution_type_adapters

        contribution_types = contribution_type_adapters()
        self.data_directory.mkdir(parents=True, exist_ok=True)
        await self.client.start()
        initialization = await self.client.request(
            "initialize",
            {
                "protocol_version": self.manifest.runtime.protocol,
                "plugin_api": PLUGIN_API_VERSION,
                "plugin_id": self.manifest.plugin_id,
                "package_digest": self.package_digest,
                "grants": sorted(self.permissions),
                "configuration": self.configuration,
                "data_directory": str(self.data_directory),
                "credential_environment": self.credential_environment,
                "features": [
                    *(
                        contribution_type.kind
                        for contribution_type in contribution_types
                        if contribution_type.worker is not None
                    ),
                    "session_inputs",
                    "session_input_batches",
                    "tool_input_requests",
                ],
                "limits": {"max_message_bytes": self.client.max_message_bytes},
            },
        )
        result = self._result(initialization, "initialize")
        negotiated = str(result.get("protocol_version", ""))
        if negotiated != self.manifest.runtime.protocol:
            raise WorkerProtocolError(
                "plugin worker protocol negotiation failed: "
                f"expected {self.manifest.runtime.protocol}, received {negotiated or '<missing>'}"
            )
        self._initialized = True
        raw = await self.client.request("contributions.prepare")
        raw = self._result(raw, "contributions.prepare")
        available = _worker_contribution_ids(raw)
        _validate_worker_declarations(self.manifest, available)
        contributions = sdk.ContributionSet()
        for contribution_type in contribution_types:
            adapter = contribution_type.worker
            if adapter is None:
                continue
            setattr(
                contributions,
                adapter.attribute,
                adapter.prepare(self, raw, available),
            )
        return contributions

    async def start(self) -> None:
        if self._initialized:
            await self.client.request("lifecycle.start")

    async def stop(self, reason: sdk.StopReason, deadline: float) -> None:
        del deadline
        if self._initialized:
            with suppress(Exception):
                await self.client.request("lifecycle.stop", {"reason": reason}, timeout=2.0)
        await self.client.close()

    def _result(self, raw: object, method: str) -> dict[str, Any]:
        try:
            result = _object_result(raw, method)
            _validate_worker_result(method, result)
            return result
        except WorkerProtocolError as exc:
            self.client.report_invalid_result(exc)
            raise

    def _tool_handler(self, identity: str):
        async def handler(call: sdk.ToolCall) -> sdk.ToolOutput | sdk.ToolInputRequest:
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
            result = self._result(raw, "tool.call")
            if "input_request" in result:
                return sdk.ToolInputRequest(data=result["input_request"])
            return sdk.ToolOutput(
                content=str(result.get("content", "")),
                structured=_object_value(result.get("structured", {})),
                resource_refs=tuple(str(item) for item in result.get("resource_refs", [])),
                is_error=bool(result.get("is_error", False)),
                session_control_transitions=_worker_control_transitions(result),
            )

        return handler

    def _tool_resume_handler(self, identity: str):
        async def handler(
            call: sdk.ToolResumeCall,
        ) -> sdk.ToolOutput | sdk.ToolInputRequest:
            raw = await self.client.request(
                "tool.resume",
                {
                    "contribution_id": identity,
                    "call": {
                        "tool_call_id": call.tool_call_id,
                        "session_id": call.session_id,
                        "run_id": call.run_id,
                        "tool_name": call.tool_name,
                        "arguments": dict(call.arguments),
                        "request": call.request,
                        "resolution": _worker_input_resolution(call.resolution),
                        "workspace": call.workspace,
                        "workspace_only": call.workspace_only,
                    },
                },
            )
            result = self._result(raw, "tool.resume")
            if "input_request" in result:
                return sdk.ToolInputRequest(data=result["input_request"])
            return sdk.ToolOutput(
                content=str(result.get("content", "")),
                structured=_object_value(result.get("structured", {})),
                resource_refs=tuple(str(item) for item in result.get("resource_refs", [])),
                is_error=bool(result.get("is_error", False)),
                session_control_transitions=_worker_control_transitions(result),
            )

        return handler

    def _observer_handler(self, identity: str):
        async def handler(observation: sdk.Observation) -> None:
            raw = await self.client.request(
                "observer.observe",
                {
                    "contribution_id": identity,
                    "observation": _worker_observation_payload(observation),
                },
            )
            self._result(raw, "observer.observe")

        return handler

    def _context_handler(self, identity: str):
        async def handler(call: sdk.ContextCall) -> sdk.ContextOutput:
            raw = await self.client.request(
                "context.call",
                {
                    "contribution_id": identity,
                    "call": {
                        "session_id": call.session_id,
                        "run_id": call.run_id,
                        "message_count": call.message_count,
                        "model_round": call.model_round,
                    },
                },
            )
            result = self._result(raw, "context.call")
            return sdk.ContextOutput(
                fragments=tuple(
                    _worker_context_fragment(item) for item in result.get("fragments", [])
                ),
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
            result = self._result(raw, "command.call")
            return sdk.CommandOutput(
                output=str(result.get("output", "")),
                data=_object_value(result.get("data", {})),
                session_inputs=_worker_session_inputs(result),
                session_input_batches=_worker_session_input_batches(result),
                session_control_transitions=_worker_control_transitions(result),
            )

        return handler

    def _session_controller_handler(self, identity: str):
        async def handler(call: sdk.SessionControllerCall) -> sdk.SessionControllerOutput:
            raw = await self.client.request(
                "session_controller.call",
                {
                    "contribution_id": identity,
                    "call": {
                        "session_id": call.session_id,
                        "session_kind": call.session_kind,
                        "activation_id": call.activation_id,
                        "controller_id": call.controller_id,
                        "action": call.action,
                        "payload": call.payload,
                        "reason": call.reason,
                        "workspace": call.workspace,
                    },
                },
            )
            result = self._result(raw, "session_controller.call")
            owner_revision = result.get("owner_state_revision")
            return sdk.SessionControllerOutput(
                output=str(result.get("output", "")),
                data=_object_value(result.get("data", {})),
                owner_state_revision=(owner_revision if isinstance(owner_revision, int) else None),
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
            result = self._result(raw, "compaction.call")
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
            result = self._result(raw, "continuation.call")
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
                role=_worker_session_input_role(result.get("role", "system")),
                scaffold=_worker_scaffold(result.get("scaffold", True)),
                semantic_tag=_worker_semantic_tag(result.get("semantic_tag")),
                coalesce_key=(
                    str(result["coalesce_key"])
                    if result.get("coalesce_key") is not None
                    else None
                ),
                reason=(str(result["reason"]) if result.get("reason") is not None else None),
                state_revision=revision,
                session_control_transitions=tuple(_worker_control_transitions(result)),
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
                        "canonical_tool_id": call.canonical_tool_id,
                        "owner_id": call.owner_id,
                        "arguments": dict(call.arguments),
                        "source": call.source,
                        "effect": call.effect,
                        "batch_behavior": call.batch_behavior,
                        "permission_control": call.permission_control,
                        "workspace": call.workspace,
                    },
                },
            )
            result = self._result(raw, "tool_policy.call")
            action = result.get("action", "ignore")
            if action not in {"ignore", "require_approval", "deny"}:
                raise WorkerProtocolError(f"worker tool policy action is invalid: {action}")
            reason = result.get("reason")
            if reason is not None and not isinstance(reason, str):
                raise WorkerProtocolError("worker tool policy reason must be a string")
            return sdk.ToolPolicyDecision(action=action, reason=reason)

        return handler

    def _prompt_policy_handler(self, identity: str):
        async def handler(call: sdk.PromptPolicyCall) -> sdk.PromptPolicyDecision:
            raw = await self.client.request(
                "prompt_policy.call",
                {
                    "contribution_id": identity,
                    "call": {
                        "session_id": call.session_id,
                        "prospective_run_id": call.prospective_run_id,
                        "prompt": call.prompt,
                        "input_source": call.input_source,
                        "workspace": call.workspace,
                        "attachments": [
                            {
                                "resource_id": item.resource_id,
                                "media_type": item.media_type,
                                "size": item.size,
                            }
                            for item in call.attachments
                        ],
                        "skill_activation": (
                            {
                                "name": call.skill_activation.name,
                                "source": call.skill_activation.source,
                                "location": call.skill_activation.location,
                                "content_digest": call.skill_activation.content_digest,
                            }
                            if call.skill_activation is not None
                            else None
                        ),
                        "catalog_id": call.catalog_id,
                        "execution_contracts": dict(call.execution_contracts),
                    },
                },
            )
            result = self._result(raw, "prompt_policy.call")
            action = result.get("action", "allow")
            if action not in {"allow", "deny"}:
                raise WorkerProtocolError(f"worker prompt policy action is invalid: {action}")
            reason = result.get("reason")
            if reason is not None and not isinstance(reason, str):
                raise WorkerProtocolError("worker prompt policy reason must be a string")
            return sdk.PromptPolicyDecision(action=action, reason=reason)

        return handler

    def _turn_policy_handler(self, identity: str):
        async def handler(call: sdk.TurnCompletionCall) -> sdk.TurnPolicyDecision:
            raw = await self.client.request(
                "turn_policy.call",
                {
                    "contribution_id": identity,
                    "call": {
                        "session_id": call.session_id,
                        "run_id": call.run_id,
                        "turn_id": call.turn_id,
                        "assistant_text": call.assistant_text,
                        "assistant_reasoning": call.assistant_reasoning,
                    },
                },
            )
            result = self._result(raw, "turn_policy.call")
            action = result.get("action", "allow")
            if action not in {"allow", "continue"}:
                raise WorkerProtocolError(f"worker turn policy action is invalid: {action}")
            feedback = result.get("feedback")
            if feedback is not None and not isinstance(feedback, str):
                raise WorkerProtocolError("worker turn policy feedback must be a string")
            return sdk.TurnPolicyDecision(action=action, feedback=feedback)

        return handler


WorkerContributionBuilder = Callable[
    [WorkerPluginInstance, object, dict[str, set[str]]],
    list[Any],
]


@dataclass(frozen=True)
class WorkerContributionAdapter:
    kind: str
    attribute: str
    build: WorkerContributionBuilder

    def prepare(
        self,
        instance: WorkerPluginInstance,
        raw: object,
        available: dict[str, set[str]],
    ) -> list[Any]:
        return self.build(instance, raw, available)


def _worker_tools(
    instance: WorkerPluginInstance,
    raw: object,
    available: dict[str, set[str]],
) -> list[Any]:
    return [
        sdk.ToolContribution(
            id=item.id,
            exposed_name=item.exposed_name,
            description=item.description,
            permission_mode=item.permission_mode,
            permission_control=item.permission_control,
            effect=item.effect,
            batch_behavior=item.batch_behavior,
            parameters_schema=_worker_schema(raw, "tools", item.id, "parameters_schema"),
            result_schema=_worker_schema(raw, "tools", item.id, "result_schema"),
            handler=instance._tool_handler(item.id),
            resume_handler=(instance._tool_resume_handler(item.id) if item.resumable else None),
            supports_unattended=item.supports_unattended,
            active_when=item.active_when,
            interaction_controller=item.interaction_controller,
        )
        for item in instance.manifest.contributes.tools
        if item.id in available.get("tools", set())
    ]


def _worker_observers(
    instance: WorkerPluginInstance,
    raw: object,
    available: dict[str, set[str]],
) -> list[Any]:
    del raw
    return [
        sdk.ObserverContribution(
            id=item.id,
            event=item.event,
            priority=item.priority,
            timeout_seconds=item.timeout_seconds,
            event_types=tuple(item.event_types),
            active_when=item.active_when,
            handler=instance._observer_handler(item.id),
        )
        for item in instance.manifest.contributes.observers
        if item.id in available.get("observers", set())
    ]


def _worker_commands(
    instance: WorkerPluginInstance,
    raw: object,
    available: dict[str, set[str]],
) -> list[Any]:
    del raw
    return [
        sdk.CommandContribution(
            id=item.id,
            description=item.description,
            handler=instance._command_handler(item.id),
        )
        for item in instance.manifest.contributes.commands
        if item.id in available.get("commands", set())
    ]


def _worker_session_controllers(
    instance: WorkerPluginInstance,
    raw: object,
    available: dict[str, set[str]],
) -> list[Any]:
    del raw
    return [
        sdk.SessionControllerContribution(
            id=item.id,
            description=item.description,
            claims=tuple(
                sdk.SessionControlClaim(domain=claim.domain, access=claim.access)
                for claim in item.claims
            ),
            activate=instance._session_controller_handler(item.id),
            deactivate=instance._session_controller_handler(item.id),
            timeout_seconds=item.timeout_seconds,
        )
        for item in instance.manifest.contributes.session_controllers
        if item.id in available.get("session_controllers", set())
    ]


def _worker_context_providers(
    instance: WorkerPluginInstance,
    raw: object,
    available: dict[str, set[str]],
) -> list[Any]:
    del raw
    return [
        sdk.ContextContribution(
            id=item.id,
            handler=instance._context_handler(item.id),
            priority=item.priority,
            timeout_seconds=item.timeout_seconds,
            failure_mode=item.failure_mode,
            active_when=item.active_when,
        )
        for item in instance.manifest.contributes.context_providers
        if item.id in available.get("context_providers", set())
    ]


def _worker_compaction_strategies(
    instance: WorkerPluginInstance,
    raw: object,
    available: dict[str, set[str]],
) -> list[Any]:
    del raw
    return [
        sdk.CompactionContribution(
            id=item.id,
            version="1",
            handler=instance._compaction_handler(item.id),
        )
        for item in instance.manifest.contributes.compaction_strategies
        if item.id in available.get("compaction_strategies", set())
    ]


def _worker_background_services(
    instance: WorkerPluginInstance,
    raw: object,
    available: dict[str, set[str]],
) -> list[Any]:
    del raw
    return [
        sdk.BackgroundServiceContribution(
            id=item.id,
            start=_noop_background_lifecycle,
            stop=_noop_background_lifecycle,
        )
        for item in instance.manifest.contributes.background_services
        if item.id in available.get("background_services", set())
    ]


def _worker_continuation_policies(
    instance: WorkerPluginInstance,
    raw: object,
    available: dict[str, set[str]],
) -> list[Any]:
    del raw
    return [
        sdk.ContinuationPolicyContribution(
            id=item.id,
            handler=instance._continuation_handler(item.id),
            priority=item.priority,
            timeout_seconds=item.timeout_seconds,
            active_when=item.active_when,
        )
        for item in instance.manifest.contributes.continuation_policies
        if item.id in available.get("continuation_policies", set())
    ]


def _worker_tool_policies(
    instance: WorkerPluginInstance,
    raw: object,
    available: dict[str, set[str]],
) -> list[Any]:
    del raw
    return [
        sdk.ToolPolicyContribution(
            id=item.id,
            handler=instance._tool_policy_handler(item.id),
            priority=item.priority,
            timeout_seconds=item.timeout_seconds,
            active_when=item.active_when,
        )
        for item in instance.manifest.contributes.tool_policies
        if item.id in available.get("tool_policies", set())
    ]


def _worker_prompt_policies(
    instance: WorkerPluginInstance,
    raw: object,
    available: dict[str, set[str]],
) -> list[Any]:
    del raw
    return [
        sdk.PromptPolicyContribution(
            id=item.id,
            handler=instance._prompt_policy_handler(item.id),
            priority=item.priority,
            timeout_seconds=item.timeout_seconds,
            active_when=item.active_when,
        )
        for item in instance.manifest.contributes.prompt_policies
        if item.id in available.get("prompt_policies", set())
    ]


def _worker_turn_policies(
    instance: WorkerPluginInstance,
    raw: object,
    available: dict[str, set[str]],
) -> list[Any]:
    del raw
    return [
        sdk.TurnPolicyContribution(
            id=item.id,
            handler=instance._turn_policy_handler(item.id),
            priority=item.priority,
            timeout_seconds=item.timeout_seconds,
            active_when=item.active_when,
        )
        for item in instance.manifest.contributes.turn_policies
        if item.id in available.get("turn_policies", set())
    ]


TOOL_WORKER_ADAPTER = WorkerContributionAdapter("tools", "tools", _worker_tools)
OBSERVER_WORKER_ADAPTER = WorkerContributionAdapter(
    "observers",
    "observers",
    _worker_observers,
)
COMMAND_WORKER_ADAPTER = WorkerContributionAdapter(
    "commands",
    "commands",
    _worker_commands,
)
CONTEXT_WORKER_ADAPTER = WorkerContributionAdapter(
    "context_providers",
    "context_providers",
    _worker_context_providers,
)
COMPACTION_WORKER_ADAPTER = WorkerContributionAdapter(
    "compaction_strategies",
    "compaction_strategies",
    _worker_compaction_strategies,
)
BACKGROUND_SERVICE_WORKER_ADAPTER = WorkerContributionAdapter(
    "background_services",
    "background_services",
    _worker_background_services,
)
CONTINUATION_WORKER_ADAPTER = WorkerContributionAdapter(
    "continuation_policies",
    "continuation_policies",
    _worker_continuation_policies,
)
PROMPT_POLICY_WORKER_ADAPTER = WorkerContributionAdapter(
    "prompt_policies",
    "prompt_policies",
    _worker_prompt_policies,
)
TOOL_POLICY_WORKER_ADAPTER = WorkerContributionAdapter(
    "tool_policies",
    "tool_policies",
    _worker_tool_policies,
)
TURN_POLICY_WORKER_ADAPTER = WorkerContributionAdapter(
    "turn_policies",
    "turn_policies",
    _worker_turn_policies,
)
SESSION_CONTROLLER_WORKER_ADAPTER = WorkerContributionAdapter(
    "session_controllers",
    "session_controllers",
    _worker_session_controllers,
)


async def _terminate_process_tree(
    process: asyncio.subprocess.Process,
    *,
    timeout: float,
) -> None:
    if os.name == "nt":
        killer: asyncio.subprocess.Process | None = None
        try:
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            # `timeout` controls the worker-facing close budget.  The OS tree
            # terminator needs its own small cleanup budget: abandoning it can
            # leave descendants holding the worker pipe handles open.
            await asyncio.wait_for(killer.wait(), timeout=2.0)
        except (OSError, TimeoutError):
            if killer is not None and killer.returncode is None:
                with suppress(ProcessLookupError):
                    killer.kill()
                await killer.wait()
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
        await process.wait()
        return

    with suppress(ProcessLookupError):
        os.kill(-process.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout)
    except TimeoutError:
        with suppress(ProcessLookupError):
            os.kill(-process.pid, 9)
        if process.returncode is None:
            process.kill()
        await process.wait()


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
    from ouro.plugins.contribution_types import contribution_type_adapters

    declarations_by_kind = manifest.contributes.by_kind()
    worker_kinds = {
        contribution_type.kind
        for contribution_type in contribution_type_adapters()
        if contribution_type.worker is not None
    }
    unknown = set(available) - worker_kinds
    if unknown:
        raise WorkerProtocolError(f"worker returned unknown contribution types: {sorted(unknown)}")
    for contribution_type in contribution_type_adapters():
        if contribution_type.worker is None:
            continue
        kind = contribution_type.kind
        declarations = declarations_by_kind[kind]
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


def _worker_observation_payload(observation: sdk.Observation) -> dict[str, object]:
    if isinstance(observation, sdk.RuntimeEventObservation):
        record = observation.record
        return {
            "event": observation.event,
            "session_id": observation.session_id,
            "run_id": observation.run_id,
            "record": {
                "event_id": record.event_id,
                "session_id": record.session_id,
                "run_id": record.run_id,
                "turn_id": record.turn_id,
                "event_type": record.event_type,
                "created_at": record.created_at.isoformat(),
                "sequence": record.sequence,
                "payload": _wire_json(record.payload),
                "metadata": _wire_json(record.metadata),
            },
        }
    if isinstance(observation, sdk.ContextPreparedObservation):
        return {
            "event": observation.event,
            "session_id": observation.session_id,
            "run_id": observation.run_id,
            "message_count": observation.message_count,
            "tool_count": observation.tool_count,
            "model_round": observation.model_round,
            "context_metadata": _wire_json(observation.context_metadata),
        }
    if isinstance(observation, sdk.ToolCallRequestedObservation):
        return {
            "event": observation.event,
            "session_id": observation.session_id,
            "run_id": observation.run_id,
            "tool_call_id": observation.tool_call_id,
            "tool_name": observation.tool_name,
            "canonical_tool_id": observation.canonical_tool_id,
            "owner_id": observation.owner_id,
            "effect": observation.effect,
            "batch_behavior": observation.batch_behavior,
            "arguments": _wire_json(observation.arguments),
        }
    if isinstance(observation, sdk.ToolCallFinishedObservation):
        return {
            "event": observation.event,
            "session_id": observation.session_id,
            "run_id": observation.run_id,
            "tool_call_id": observation.tool_call_id,
            "tool_name": observation.tool_name,
            "canonical_tool_id": observation.canonical_tool_id,
            "owner_id": observation.owner_id,
            "effect": observation.effect,
            "batch_behavior": observation.batch_behavior,
            "is_error": observation.is_error,
            "content": observation.content,
            "structured": _wire_json(observation.structured),
            "resource_refs": list(observation.resource_refs),
            "exit_code": observation.exit_code,
        }
    if isinstance(observation, sdk.RunFinishedObservation):
        outcome = observation.outcome
        return {
            "event": observation.event,
            "session_id": observation.session_id,
            "run_id": observation.run_id,
            "outcome": {
                "session_id": outcome.session_id,
                "run_id": outcome.run_id,
                "status": outcome.status,
                "origin": outcome.origin,
                "final_text": outcome.final_text,
                "error": outcome.error,
                "usage": dict(outcome.usage),
            },
        }
    raise WorkerProtocolError(f"unsupported worker observation: {type(observation).__name__}")


def _wire_json(value: object) -> object:
    if isinstance(value, dict | Mapping):
        return {str(key): _wire_json(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_wire_json(item) for item in value]
    return value


def _worker_input_resolution(resolution: sdk.ToolInputResolution) -> dict[str, object]:
    if isinstance(resolution, sdk.AnswerResolution):
        return {
            "kind": "answer",
            "value": resolution.value,
            "resolved_by": resolution.resolved_by,
            "resolver_id": resolution.resolver_id,
        }
    if isinstance(resolution, sdk.UnattendedResolution):
        return {
            "kind": "unattended",
            "resolved_by": "host_policy",
            "resolver_id": resolution.resolver_id,
            "reason": resolution.reason,
        }
    raise WorkerProtocolError(f"unsupported tool input resolution: {type(resolution).__name__}")


def _validate_worker_result(method: str, result: dict[str, Any]) -> None:
    if method == "initialize":
        if not isinstance(result.get("protocol_version"), str):
            raise WorkerProtocolError("worker initialize protocol_version must be a string")
        return
    if method == "contributions.prepare":
        for kind, values in result.items():
            if isinstance(values, list):
                if any(not isinstance(item, str) for item in values):
                    raise WorkerProtocolError(f"worker {kind} contribution ids must be strings")
                continue
            if not isinstance(values, dict) or any(
                not isinstance(identity, str) or not isinstance(descriptor, dict)
                for identity, descriptor in values.items()
            ):
                raise WorkerProtocolError(
                    f"worker {kind} contributions must be string ids or descriptor objects"
                )
            if kind == "tools":
                for descriptor in values.values():
                    for field in ("parameters_schema", "result_schema"):
                        value = descriptor.get(field, {})
                        if not isinstance(value, dict):
                            raise WorkerProtocolError(f"worker tool {field} must be an object")
        return
    if method in {"tool.call", "tool.resume"}:
        if "input_request" in result:
            output_fields = {"content", "structured", "resource_refs", "is_error"}
            if output_fields.intersection(result):
                raise WorkerProtocolError(
                    f"worker {method} cannot return input_request together with tool output"
                )
            return
        _require_optional_type(result, "content", str, method)
        _require_optional_type(result, "structured", dict, method)
        _require_string_list(result, "resource_refs", method)
        _require_optional_type(result, "is_error", bool, method)
        _worker_control_transitions(result)
        return
    if method == "observer.observe":
        if result:
            raise WorkerProtocolError("worker observer.observe result must be empty")
        return
    if method == "context.call":
        fragments = result.get("fragments", [])
        if not isinstance(fragments, list):
            raise WorkerProtocolError("worker context.call fragments must be a list")
        for item in fragments:
            _worker_context_fragment(item)
        return
    if method == "command.call":
        _require_optional_type(result, "output", str, method)
        _require_optional_type(result, "data", dict, method)
        _worker_session_inputs(result)
        _worker_session_input_batches(result)
        _worker_control_transitions(result)
        return
    if method == "session_controller.call":
        _require_optional_type(result, "output", str, method)
        _require_optional_type(result, "data", dict, method)
        _require_optional_type(
            result,
            "owner_state_revision",
            int,
            method,
            allow_none=True,
        )
        return
    if method == "compaction.call":
        _require_optional_type(result, "summary", str, method)
        usage = result.get("usage", {})
        if not isinstance(usage, dict) or any(
            isinstance(value, bool) or not isinstance(value, (int, float))
            for value in usage.values()
        ):
            raise WorkerProtocolError("worker compaction.call usage must be numeric")
        return
    if method == "continuation.call":
        action = result.get("action", "ignore")
        if action not in {"ignore", "continue", "complete", "pause", "fail"}:
            raise WorkerProtocolError(f"worker continuation action is invalid: {action}")
        mode = result.get("mode", "follow_up")
        if mode not in {"follow_up", "steer"}:
            raise WorkerProtocolError(f"worker continuation mode is invalid: {mode}")
        _require_optional_type(result, "prompt", str, method, allow_none=True)
        _require_optional_type(result, "title", str, method, allow_none=True)
        _require_optional_type(result, "role", str, method, allow_none=True)
        _require_optional_type(result, "scaffold", bool, method)
        _require_optional_type(result, "semantic_tag", str, method, allow_none=True)
        _require_optional_type(result, "coalesce_key", str, method, allow_none=True)
        _require_optional_type(result, "reason", str, method, allow_none=True)
        _require_optional_type(result, "state_revision", int, method, allow_none=True)
        _worker_control_transitions(result)
        return
    if method == "tool_policy.call":
        action = result.get("action", "ignore")
        if action not in {"ignore", "require_approval", "deny"}:
            raise WorkerProtocolError(f"worker tool policy action is invalid: {action}")
        _require_optional_type(result, "reason", str, method, allow_none=True)
        return
    if method == "prompt_policy.call":
        action = result.get("action", "allow")
        if action not in {"allow", "deny"}:
            raise WorkerProtocolError(f"worker prompt policy action is invalid: {action}")
        _require_optional_type(result, "reason", str, method, allow_none=True)
        return
    if method == "turn_policy.call":
        action = result.get("action", "allow")
        if action not in {"allow", "continue"}:
            raise WorkerProtocolError(f"worker turn policy action is invalid: {action}")
        _require_optional_type(result, "feedback", str, method, allow_none=True)


def _require_optional_type(
    result: dict[str, Any],
    field: str,
    expected: type[object],
    method: str,
    *,
    allow_none: bool = False,
) -> None:
    if field not in result:
        return
    value = result[field]
    if allow_none and value is None:
        return
    if not isinstance(value, expected) or (expected is int and isinstance(value, bool)):
        raise WorkerProtocolError(f"worker {method} {field} must be {expected.__name__}")


def _require_string_list(result: dict[str, Any], field: str, method: str) -> None:
    value = result.get(field, [])
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise WorkerProtocolError(f"worker {method} {field} must be a string array")


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
        role = value.get("role", "system")
        scaffold = _worker_scaffold(value.get("scaffold", True))
        semantic_tag = _worker_semantic_tag(value.get("semantic_tag"))
        if not isinstance(prompt, str) or not prompt.strip():
            raise WorkerProtocolError("worker session input prompt must not be empty")
        if mode not in {"follow_up", "steer"}:
            raise WorkerProtocolError(f"worker session input mode is invalid: {mode}")
        if title is not None and not isinstance(title, str):
            raise WorkerProtocolError("worker session input title must be a string")
        role = _worker_session_input_role(role)
        inputs.append(
            sdk.SessionInput(
                prompt=prompt,
                mode=mode,
                title=title,
                role=role,
                scaffold=scaffold,
                semantic_tag=semantic_tag,
            )
        )
    return tuple(inputs)


def _worker_session_input_batches(
    raw: dict[str, Any],
) -> tuple[sdk.SessionInputBatch, ...]:
    values = raw.get("session_input_batches", [])
    if not isinstance(values, list):
        raise WorkerProtocolError("worker session_input_batches result must be a list")
    batches: list[sdk.SessionInputBatch] = []
    for value in values:
        if not isinstance(value, dict):
            raise WorkerProtocolError("worker session input batch must be an object")
        coalesce_key = value.get("coalesce_key")
        if coalesce_key is not None and (
            not isinstance(coalesce_key, str) or not coalesce_key.strip()
        ):
            raise WorkerProtocolError("worker session input batch coalesce_key must not be empty")
        batches.append(
            sdk.SessionInputBatch(
                inputs=_worker_session_inputs({"session_inputs": value.get("inputs", [])}),
                coalesce_key=coalesce_key,
            )
        )
    return tuple(batches)


def _worker_session_input_role(value: object) -> sdk.SessionInputRole:
    if value not in {"system", "user", "assistant", "tool_result"}:
        raise WorkerProtocolError(f"worker session input role is invalid: {value}")
    return value


def _worker_scaffold(value: object) -> bool:
    if not isinstance(value, bool):
        raise WorkerProtocolError("worker message scaffold must be a boolean")
    return value


def _worker_semantic_tag(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise WorkerProtocolError("worker message semantic_tag must not be empty")
    try:
        return validate_semantic_tag(value)
    except ValueError as exc:
        raise WorkerProtocolError(f"worker message semantic_tag is invalid: {exc}") from exc


def _worker_control_transitions(
    raw: dict[str, Any],
) -> tuple[sdk.SessionControlTransition, ...]:
    values = raw.get("session_control_transitions", [])
    if not isinstance(values, list):
        raise WorkerProtocolError("worker session_control_transitions result must be a list")
    transitions: list[sdk.SessionControlTransition] = []
    for value in values:
        if not isinstance(value, dict):
            raise WorkerProtocolError("worker session control transition must be an object")
        try:
            transitions.append(
                sdk.SessionControlTransition(
                    action=value["action"],
                    controller_id=value.get("controller_id"),
                    activation_id=value.get("activation_id"),
                    replace_activation_ids=tuple(value.get("replace_activation_ids", [])),
                    expected_session_revision=value.get("expected_session_revision"),
                    expected_activation_revision=value.get("expected_activation_revision"),
                    payload=value.get("payload"),
                    reason=value.get("reason"),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise WorkerProtocolError(
                f"worker session control transition is invalid: {exc}"
            ) from exc
    return tuple(transitions)


def _worker_context_fragment(raw: object) -> str | sdk.ContextFragment:
    if isinstance(raw, str):
        return raw
    if not isinstance(raw, dict):
        raise WorkerProtocolError("worker context fragment must be a string or object")
    text = raw.get("text")
    persistence = raw.get("persistence", "ephemeral")
    target = raw.get("target", "message")
    role = _worker_session_input_role(raw.get("role", "system"))
    scaffold = _worker_scaffold(raw.get("scaffold", True))
    semantic_tag = _worker_semantic_tag(raw.get("semantic_tag"))
    if not isinstance(text, str) or not text:
        raise WorkerProtocolError("worker context fragment text must not be empty")
    if persistence not in {"ephemeral", "message"}:
        raise WorkerProtocolError(
            "worker context fragment persistence must be ephemeral or message"
        )
    if target not in {"message", "system_prompt"}:
        raise WorkerProtocolError("worker context fragment target is invalid")
    return sdk.ContextFragment(
        text=text,
        persistence=persistence,
        target=target,
        role=role,
        scaffold=scaffold,
        semantic_tag=semantic_tag,
    )


async def _noop_background_lifecycle() -> None:
    # Worker background resources are owned by lifecycle.start/stop.  These
    # descriptors provide stable identities to the host contribution contract.
    return None


def _credential_environment_name(plugin_id: str, slot: str) -> str:
    digest = hashlib.sha256(f"{plugin_id}\0{slot}".encode()).hexdigest()[:20].upper()
    return f"OURO_PLUGIN_CREDENTIAL_{digest}"
