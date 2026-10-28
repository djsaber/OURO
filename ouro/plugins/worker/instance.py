"""Worker plugin initialization, invocation and lifecycle."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import asdict
from pathlib import Path
from time import monotonic
from typing import Any

from ouro.core.callbacks import CallbackCancelled
from ouro.plugins.installation.package import PLUGIN_API_VERSION
from ouro.plugins.manifest import PluginManifest
from ouro.plugins.tool_groups import PluginToolGroups, ToolGroupPublication
from ouro.plugins.worker.client import JsonRpcWorkerClient
from ouro.plugins.worker.codec import (
    WorkerProtocolError,
    _decode_worker_command,
    _decode_worker_compaction,
    _decode_worker_context,
    _decode_worker_continuation,
    _decode_worker_contributions,
    _decode_worker_initialization,
    _decode_worker_observer,
    _decode_worker_prompt_policy,
    _decode_worker_session_controller,
    _decode_worker_tool,
    _decode_worker_tool_policy,
    _decode_worker_turn_policy,
    _object_result,
    _worker_input_resolution,
    _worker_observation_payload,
)
from ouro.plugins.worker.drivers import WorkerDriverBridge
from ouro.plugins.worker.models import WorkerModelBridge
from ouro.plugins.worker.processes import WorkerProcessBridge
from ouro.plugins.worker.providers import WorkerProviderBridge
from ouro.plugins.worker.scheduling import WorkerSchedulerBridge
from ouro.plugins.worker.services import WorkerServiceBridge
from ouro.plugins.worker.tool_groups import WorkerToolGroupBridge
from ouro.sdk.plugins import v5 as sdk


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
        services: sdk.PluginServices | None = None,
        processes: sdk.PluginProcesses | None = None,
        environment: sdk.PluginEnvironment | None = None,
        models: sdk.PluginModels | None = None,
        inputs: sdk.RunInputs | None = None,
        tools: sdk.RunTools | None = None,
        tool_groups: PluginToolGroups | None = None,
        scheduler: sdk.PluginScheduler | None = None,
    ) -> None:
        self.manifest = manifest
        self.package_digest = package_digest
        self.permissions = permissions
        self.configuration = dict(configuration)
        self.data_directory = data_directory
        self.credentials = dict(credentials)
        self.environment = environment
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
        self.service_bridge = (
            WorkerServiceBridge(self.client, services) if services is not None else None
        )
        self.process_bridge = (
            WorkerProcessBridge(self.client, processes) if processes is not None else None
        )
        self.provider_bridge = WorkerProviderBridge(self.client)
        self.driver_bridge = WorkerDriverBridge(
            self.client, models=models, tools=tools, inputs=inputs
        )
        self.model_bridge = WorkerModelBridge(self.client, models) if models is not None else None
        self.tool_group_bridge = (
            WorkerToolGroupBridge(
                self.client, tool_groups, self._tool_handler, self._tool_resume_handler, diagnostic
            )
            if tool_groups is not None
            else None
        )
        self.initial_tool_groups: list[ToolGroupPublication] = []
        self.scheduler_bridge = WorkerSchedulerBridge(scheduler) if scheduler is not None else None
        self.client.request_handler = self._host_request
        self._initialized = False

    async def _host_request(self, method: str, params: Mapping[str, object]) -> object:
        if method == "providers.output":
            await self.provider_bridge.output(params)
            return None
        if method == "models.generate" and self.model_bridge is not None:
            return await self.model_bridge.generate(params)
        if method.startswith("scheduler.") and self.scheduler_bridge is not None:
            return await self.scheduler_bridge.dispatch(method, params)
        if method.startswith("tool_groups.") and self.tool_group_bridge is not None:
            return await self.tool_group_bridge.dispatch(method, params)
        if method.startswith("run."):
            return await self.driver_bridge.dispatch(method, params)
        if method == "environment.agent_instructions" and self.environment is not None:
            workspace = params.get("workspace")
            if workspace is not None and not isinstance(workspace, str):
                raise ValueError("workspace must be a string or null")
            return [asdict(item) for item in await self.environment.agent_instructions(workspace)]
        if method == "processes.run" and self.process_bridge is not None:
            return await self.process_bridge.run(params)
        if self.service_bridge is not None:
            return await self.service_bridge.dispatch(method, params)
        raise ValueError(f"Host method is unavailable: {method}")

    @property
    def is_healthy(self) -> bool:
        return self._initialized and self.client.is_healthy

    async def prepare(self) -> sdk.ContributionSet:
        from ouro.plugins.contributions.inventory import contribution_type_adapters

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
                    "command_run_actions",
                    "tool_input_requests",
                    *(["services", "host_requests"] if self.service_bridge is not None else []),
                    *(["processes"] if self.process_bridge is not None else []),
                    *(["environment"] if self.environment is not None else []),
                    *(["tool_groups"] if self.tool_group_bridge is not None else []),
                    *(["scheduler"] if self.scheduler_bridge is not None else []),
                    *(["models"] if self.model_bridge is not None else []),
                ],
                "limits": {
                    "max_message_bytes": self.client.max_message_bytes,
                    "max_incoming_requests": self.client.max_incoming_requests,
                    "max_pending_requests": self.client.max_pending_requests,
                },
            },
        )
        result = self._result(initialization, "initialize", _decode_worker_initialization)
        negotiated = str(result.get("protocol_version", ""))
        if negotiated != self.manifest.runtime.protocol:
            raise WorkerProtocolError(
                "plugin worker protocol negotiation failed: "
                f"expected {self.manifest.runtime.protocol}, received {negotiated or '<missing>'}"
            )
        self._initialized = True
        raw = await self.client.request("contributions.prepare")
        raw = self._result(raw, "contributions.prepare", _decode_worker_contributions)
        available = _worker_contribution_ids(raw)
        _validate_worker_declarations(self.manifest, available)
        contributions = sdk.ContributionSet()
        if self.tool_group_bridge is not None:
            self.initial_tool_groups = self.tool_group_bridge.prepare(raw.get("tool_groups", {}))
        if self.service_bridge is not None:
            contributions.services = self.service_bridge.contributions(self.manifest.exports)
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

    def _background_handler(self, service_id: str, *, start: bool) -> sdk.BackgroundHandler:
        async def handler() -> None:
            if not start and not self.client.is_healthy:
                await self.client.close()
                return
            await self.client.request(
                "background.start" if start else "background.stop",
                {"contribution_id": service_id},
            )

        return handler

    async def stop(self, reason: sdk.StopReason, deadline: float) -> None:
        try:
            if self._initialized:
                with suppress(Exception):
                    await self.client.request(
                        "lifecycle.stop",
                        {"reason": reason},
                        timeout=max(0.01, min(2.0, deadline - monotonic())),
                    )
        finally:
            await self.client.close(timeout=max(0.01, deadline - monotonic()))
            if self.tool_group_bridge is not None:
                await self.tool_group_bridge.close()

    def _result[T](
        self,
        raw: object,
        method: str,
        decode: Callable[[dict[str, Any], str], T],
        *,
        state_view: sdk.PluginSessionStateView | None = None,
    ) -> T:
        try:
            result = _object_result(raw, method)
            changed = "session_state" in result
            change = result.pop("session_state", None)
            # Decode the complete output before staging any owner state.
            output = decode(result, method)
            if changed:
                if state_view is None:
                    raise WorkerProtocolError("worker state change requires a Session invocation")
                if change is None:
                    state_view.delete()
                elif isinstance(change, dict):
                    # Validate once at the shared SDK boundary, with host-bound identity.
                    state_view.replace(**change)
                else:
                    raise WorkerProtocolError(
                        "worker session_state must be a replacement object or null"
                    )
            return output
        except WorkerProtocolError as exc:
            self.client.report_invalid_result(exc)
            raise

    async def _session_request[T](
        self,
        method: str,
        params: dict[str, object],
        *,
        decode: Callable[[dict[str, Any], str], T],
        state_view: sdk.PluginSessionStateView | None,
        timeout: float | None = None,
        retain_result_on_cancel: bool = False,
    ) -> T:
        """Exchange an owner snapshot and a staged replacement through the call protocol."""

        if state_view is not None:
            value = state_view.read()
            params = {
                **params,
                "session_state": {
                    "writable": state_view.writable,
                    "controls": [
                        {
                            "controller_id": item.controller_id,
                            "activation_id": item.activation_id,
                            "data": item.data,
                            "usage_scope": item.usage_scope,
                        }
                        for item in state_view.controls()
                    ],
                    "run_controls": [
                        {
                            "controller_id": item.controller_id,
                            "activation_id": item.activation_id,
                            "usage_scope": item.usage_scope,
                        }
                        for item in state_view.run_controls()
                    ],
                    "usage_tokens": state_view.usage_tokens(),
                    "value": (
                        {
                            "state_schema_version": value.state_schema_version,
                            "payload": value.payload,
                        }
                        if value is not None
                        else None
                    ),
                },
            }
        try:
            raw = await self.client.request(
                method, params, timeout=timeout, retain_result_on_cancel=retain_result_on_cancel
            )
        except CallbackCancelled as exc:
            result = _object_result(exc.result, method)
            result.pop("session_state", None)
            raise CallbackCancelled(decode(result, method)) from None
        return self._result(raw, method, decode, state_view=state_view)

    def _tool_handler(self, identity: str, registration_id: str | None = None):
        async def handler(call: sdk.ToolCall) -> sdk.ToolOutput | sdk.ToolInputRequest:
            return await self._session_request(
                "tool.call" if registration_id is None else "tool_groups.call",
                {
                    "contribution_id": identity,
                    **({"registration_id": registration_id} if registration_id is not None else {}),
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
                decode=_decode_worker_tool,
                state_view=call.session_state,
                timeout=0,
                retain_result_on_cancel=True,
            )

        return handler

    def _tool_resume_handler(self, identity: str, registration_id: str | None = None):
        async def handler(
            call: sdk.ToolResumeCall,
        ) -> sdk.ToolOutput | sdk.ToolInputRequest:
            return await self._session_request(
                "tool.resume" if registration_id is None else "tool_groups.resume",
                {
                    "contribution_id": identity,
                    **({"registration_id": registration_id} if registration_id is not None else {}),
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
                decode=_decode_worker_tool,
                state_view=call.session_state,
                timeout=0,
                retain_result_on_cancel=True,
            )

        return handler

    def _observer_handler(self, identity: str):
        async def handler(observation: sdk.Observation) -> None:
            await self._session_request(
                "observer.observe",
                {
                    "contribution_id": identity,
                    "observation": _worker_observation_payload(observation),
                },
                decode=_decode_worker_observer,
                state_view=observation.session_state,
            )

        return handler

    def _context_handler(self, identity: str):
        async def handler(call: sdk.ContextCall) -> sdk.ContextOutput:
            return await self._session_request(
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
                decode=_decode_worker_context,
                state_view=call.session_state,
            )

        return handler

    def _command_handler(self, identity: str):
        async def handler(call: sdk.CommandCall) -> sdk.CommandOutput:
            return await self._session_request(
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
                decode=_decode_worker_command,
                state_view=call.session_state,
            )

        return handler

    def _session_controller_handler(self, identity: str):
        async def handler(call: sdk.SessionControllerCall) -> sdk.SessionControllerOutput:
            return await self._session_request(
                "session_controller.call",
                {
                    "contribution_id": identity,
                    "call": {
                        "session_id": call.session_id,
                        "session_kind": call.session_kind,
                        "activation_id": call.activation_id,
                        "controller_id": call.controller_id,
                        "action": call.action,
                        "replacement_controller_id": call.replacement_controller_id,
                        "payload": call.payload,
                        "reason": call.reason,
                        "workspace": call.workspace,
                    },
                },
                decode=_decode_worker_session_controller,
                state_view=call.session_state,
            )

        return handler

    def _compaction_handler(self, identity: str):
        async def handler(call: sdk.CompactionCall) -> sdk.CompactionOutput:
            return await self._session_request(
                "compaction.call",
                {
                    "contribution_id": identity,
                    "call": {
                        "session_id": call.session_id,
                        "run_id": call.run_id,
                        "agent_system_prompt": call.agent_system_prompt,
                        "previous_summary": call.previous_summary,
                        "messages": [dict(message) for message in call.messages],
                    },
                },
                decode=_decode_worker_compaction,
                state_view=call.session_state,
            )

        return handler

    def _continuation_handler(self, identity: str):
        async def handler(call: sdk.RunOutcome) -> sdk.ContinuationDecision:
            return await self._session_request(
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
                        "tool_call_count": call.tool_call_count,
                        "automatic_continuation": call.automatic_continuation,
                    },
                },
                decode=_decode_worker_continuation,
                state_view=call.session_state,
            )

        return handler

    def _tool_policy_handler(self, identity: str):
        async def handler(call: sdk.ToolPolicyCall) -> sdk.ToolPolicyDecision:
            return await self._session_request(
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
                decode=_decode_worker_tool_policy,
                state_view=call.session_state,
            )

        return handler

    def _prompt_policy_handler(self, identity: str):
        async def handler(call: sdk.PromptPolicyCall) -> sdk.PromptPolicyDecision:
            return await self._session_request(
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
                decode=_decode_worker_prompt_policy,
                state_view=call.session_state,
            )

        return handler

    def _turn_policy_handler(self, identity: str):
        async def handler(call: sdk.TurnCompletionCall) -> sdk.TurnPolicyDecision:
            return await self._session_request(
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
                decode=_decode_worker_turn_policy,
                state_view=call.session_state,
            )

        return handler


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
    from ouro.plugins.contributions.inventory import contribution_type_adapters

    declarations_by_kind = manifest.contributes.by_kind()
    worker_kinds = {
        contribution_type.kind
        for contribution_type in contribution_type_adapters()
        if contribution_type.worker is not None
    }
    unknown = set(available) - worker_kinds - {"services", "tool_groups"}
    if unknown:
        raise WorkerProtocolError(f"worker returned unknown contribution types: {sorted(unknown)}")
    if available.get("services", set()) != {item.id for item in manifest.exports}:
        raise WorkerProtocolError("worker service exports do not match manifest")
    if available.get("tool_groups", set()) - {item.id for item in manifest.tool_groups}:
        raise WorkerProtocolError("worker returned undeclared tool groups")
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


def _credential_environment_name(plugin_id: str, slot: str) -> str:
    digest = hashlib.sha256(f"{plugin_id}\0{slot}".encode()).hexdigest()[:20].upper()
    return f"OURO_PLUGIN_CREDENTIAL_{digest}"
