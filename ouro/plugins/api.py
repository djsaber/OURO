from __future__ import annotations

import asyncio
import importlib
import importlib.util
import inspect
import logging
import os
import shutil
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType, ModuleType
from typing import Any
from uuid import uuid4

from ouro.core.compaction import CompactionArtifact, CompactionRequest
from ouro.core.context_providers import (
    ContextFragment,
    ContextProviderCall,
    ContextProviderRegistration,
    ContextProviderResult,
)
from ouro.core.continuations import (
    ContinuationDecision,
    ContinuationPolicyRegistration,
)
from ouro.core.hooks import HookContext, HookRegistration, HookResult
from ouro.core.interactions import (
    AnswerResolution,
    ToolInputResolution,
    UnattendedResolution,
)
from ouro.core.redaction import SensitiveValueRedactor
from ouro.core.session_controls import (
    SessionControlClaim,
    SessionControlTransitionRequest,
    SessionInputRequest,
)
from ouro.core.tool_policies import (
    ToolPolicyCall,
    ToolPolicyDecision,
    ToolPolicyRegistration,
)
from ouro.core.turn_policies import (
    TurnCompletionCandidate,
    TurnPolicyDecision,
    TurnPolicyRegistration,
)
from ouro.plugins.candidates import PluginRuntimeCandidate
from ouro.plugins.contributions import PluginContributions
from ouro.plugins.models import PluginDiagnostic, PluginGenerationRecord
from ouro.plugins.paths import portable_relative_path
from ouro.plugins.registry import (
    PluginCommand,
    PluginCommandRequest,
    PluginCommandResult,
)
from ouro.plugins.session_controllers import (
    PluginSessionController,
    PluginSessionControllerCall,
    PluginSessionControllerOutput,
)
from ouro.sdk.plugins import v1 as sdk
from ouro.tools import (
    Tool,
    ToolDefinition,
    ToolExecutionContext,
    ToolInputRequest,
    ToolInvocation,
    ToolResult,
)


class ScopedPluginDataStore:
    """Private data view whose candidate writes stay staged until publication."""

    def __init__(self, root: Path, *, staged: bool = False) -> None:
        self.root = root
        self._staged = staged
        self._changes: dict[str, bytes | None] = {}

    def read_bytes(self, relative_path: str) -> bytes:
        key = portable_relative_path(relative_path).as_posix()
        if key in self._changes:
            value = self._changes[key]
            if value is None:
                raise FileNotFoundError(relative_path)
            return value
        return self._path(relative_path).read_bytes()

    def write_bytes(self, relative_path: str, value: bytes) -> None:
        if self._staged:
            key = portable_relative_path(relative_path).as_posix()
            self._changes[key] = bytes(value)
            return
        self._write(relative_path, value)

    def delete(self, relative_path: str) -> None:
        if self._staged:
            key = portable_relative_path(relative_path).as_posix()
            self._changes[key] = None
            return
        path = self._path(relative_path)
        if path.exists():
            path.unlink()

    def activate(self) -> None:
        if not self._staged:
            return
        changes = dict(self._changes)
        self._changes.clear()
        self._staged = False
        for relative_path, value in changes.items():
            if value is None:
                self.delete(relative_path)
            else:
                self._write(relative_path, value)

    def discard(self) -> None:
        self._changes.clear()

    def _write(self, relative_path: str, value: bytes) -> None:
        path = self._path(relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(value)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _path(self, value: str) -> Path:
        relative = portable_relative_path(value)
        path = self.root.joinpath(*relative.parts).resolve(strict=False)
        root = self.root.resolve(strict=False)
        if not path.is_relative_to(root):
            raise ValueError("plugin data path escapes private data root")
        return path


class BoundCredentialResolver:
    def __init__(
        self,
        bindings: dict[str, str],
        resolve_reference: Callable[[str], str] | None,
        redactor: SensitiveValueRedactor,
    ) -> None:
        self._bindings = dict(bindings)
        self._resolve_reference = resolve_reference
        self._redactor = redactor

    def resolve(self, slot: str) -> str:
        try:
            reference = self._bindings[slot]
        except KeyError as exc:
            raise PermissionError(f"credential slot is not bound: {slot}") from exc
        if self._resolve_reference is None:
            raise RuntimeError("credential resolver is unavailable")
        value = self._resolve_reference(reference)
        self._redactor.add(value)
        return value


class OwnedTaskGroup:
    def __init__(
        self,
        on_failure: Callable[[Exception], None] | None = None,
        *,
        active: bool = True,
    ) -> None:
        self._tasks: set[asyncio.Task[object]] = set()
        self._closed = False
        self._on_failure = on_failure
        self._activated = asyncio.Event()
        if active:
            self._activated.set()

    def start(self, coroutine: Awaitable[object], *, name: str | None = None) -> None:
        if self._closed:
            if inspect.iscoroutine(coroutine):
                coroutine.close()
            raise RuntimeError("plugin task group is closed")

        async def run() -> object:
            await self._activated.wait()
            try:
                return await coroutine
            except Exception as exc:
                if self._on_failure is not None:
                    self._on_failure(exc)
                raise

        task: asyncio.Task[object] = asyncio.create_task(run(), name=name)
        self._tasks.add(task)
        task.add_done_callback(self._task_finished)

    def activate(self) -> None:
        if self._closed:
            raise RuntimeError("plugin task group is closed")
        self._activated.set()

    def _task_finished(self, task: asyncio.Task[object]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        task.exception()

    async def close(self, *, timeout: float = 5.0) -> None:
        if self._closed:
            return
        self._closed = True
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if not tasks:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=timeout,
            )
        except TimeoutError:
            return
        finally:
            self._tasks.clear()


class RedactedPluginLogger:
    def __init__(self, plugin_id: str, redactor: SensitiveValueRedactor) -> None:
        self._logger = logging.getLogger(f"ouro.plugin.{plugin_id}")
        self._redactor = redactor

    def debug(self, message: str, *args: object) -> None:
        self._log(logging.DEBUG, message, args)

    def info(self, message: str, *args: object) -> None:
        self._log(logging.INFO, message, args)

    def warning(self, message: str, *args: object) -> None:
        self._log(logging.WARNING, message, args)

    def error(self, message: str, *args: object) -> None:
        self._log(logging.ERROR, message, args)

    def _log(self, level: int, message: str, args: tuple[object, ...]) -> None:
        try:
            rendered = message % args if args else message
        except (TypeError, ValueError):
            rendered = " ".join((message, *(str(item) for item in args)))
        self._logger.log(level, self._redactor.redact(rendered))


class PluginDiagnosticEmitter:
    def __init__(
        self,
        candidate: PluginRuntimeCandidate,
        generation: PluginGenerationRecord,
        sink: list[PluginDiagnostic],
        redactor: SensitiveValueRedactor,
    ) -> None:
        self.candidate = candidate
        self.generation = generation
        self.sink = sink
        self.redactor = redactor

    def emit(
        self,
        code: str,
        message: str,
        *,
        severity: str = "warning",
    ) -> None:
        normalized_severity = severity if severity in {"info", "warning", "error"} else "warning"
        self.sink.append(
            PluginDiagnostic(
                code=code,
                phase="runtime",
                severity=normalized_severity,  # type: ignore[arg-type]
                message=self.redactor.redact(message),
                plugin_id=self.candidate.plugin_id,
                version=self.candidate.version,
                plugin_class=self.candidate.plugin_class,
                release_id=self.candidate.release_id,
                package_digest=self.candidate.package_digest,
                builtin_contract_hash=self.candidate.builtin_contract_hash,
                generation_id=self.generation.generation_id,
            )
        )


class LoadedPluginModule:
    def __init__(self, module: ModuleType, package_root: Path, imported_names: set[str]) -> None:
        self.module = module
        self.package_root = package_root
        self.imported_names = imported_names
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for name in self.imported_names:
            module = sys.modules.get(name)
            if module is not None and _module_belongs_to(module, self.package_root):
                sys.modules.pop(name, None)
        for cache in self.package_root.rglob("__pycache__"):
            shutil.rmtree(cache, ignore_errors=True)


def load_plugin_factory(
    package_root: Path,
    entrypoint: str,
    *,
    module_namespace: str,
    generation_id: str,
) -> tuple[Callable[[sdk.PluginContext], object], LoadedPluginModule]:
    module_name, _, factory_name = entrypoint.partition(":")
    top_level = module_name.partition(".")[0]
    if top_level != module_namespace:
        raise RuntimeError(
            "plugin entrypoint escaped its owned top-level module: "
            f"{top_level} != {module_namespace}"
        )
    existing = sys.modules.get(top_level)
    if existing is not None:
        raise RuntimeError(f"plugin top-level module conflicts with loaded module: {top_level}")
    private_namespace = f"_ouro_plugin_{generation_id}"
    if private_namespace in sys.modules:
        raise RuntimeError(f"plugin generation module is already loaded: {generation_id}")
    before = set(sys.modules)
    previous_bytecode_setting = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        try:
            module = _load_private_plugin_module(
                package_root,
                module_name=module_name,
                module_namespace=module_namespace,
                private_namespace=private_namespace,
            )
            if not _module_belongs_to(module, package_root):
                raise RuntimeError(f"plugin entrypoint resolved outside package: {entrypoint}")
            factory = getattr(module, factory_name, None)
            if factory is None or not callable(factory):
                raise RuntimeError(f"plugin entrypoint factory is not callable: {entrypoint}")
        except BaseException:
            for name in set(sys.modules) - before:
                loaded = sys.modules.get(name)
                if loaded is not None and _module_belongs_to(loaded, package_root):
                    sys.modules.pop(name, None)
            raise
    finally:
        sys.dont_write_bytecode = previous_bytecode_setting
    imported = {
        name
        for name in set(sys.modules) - before
        if (loaded := sys.modules.get(name)) is not None
        and _module_belongs_to(loaded, package_root)
    }
    return factory, LoadedPluginModule(module, package_root, imported)


def _load_private_plugin_module(
    package_root: Path,
    *,
    module_name: str,
    module_namespace: str,
    private_namespace: str,
) -> ModuleType:
    package_directory = package_root / module_namespace
    package_init = package_directory / "__init__.py"
    module_file = package_root / f"{module_namespace}.py"
    suffix = module_name.removeprefix(module_namespace)
    if package_init.is_file():
        spec = importlib.util.spec_from_file_location(
            private_namespace,
            package_init,
            submodule_search_locations=[str(package_directory)],
        )
        root = _execute_private_module(spec, private_namespace)
        if not suffix:
            return root
        return importlib.import_module(private_namespace + suffix)
    if suffix:
        raise RuntimeError("single-file plugin entrypoint cannot name a child module")
    spec = importlib.util.spec_from_file_location(private_namespace, module_file)
    return _execute_private_module(spec, private_namespace)


def _execute_private_module(
    spec: importlib.machinery.ModuleSpec | None,
    module_name: str,
) -> ModuleType:
    if spec is None or spec.loader is None:
        raise RuntimeError(f"plugin module cannot be loaded: {module_name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module


def build_plugin_context(
    candidate: PluginRuntimeCandidate,
    generation: PluginGenerationRecord,
    *,
    data_root: Path,
    configuration: dict[str, object],
    credential_bindings: dict[str, str],
    credential_resolver: Callable[[str], str] | None,
    tasks: OwnedTaskGroup,
    diagnostics: list[PluginDiagnostic],
    redactor: SensitiveValueRedactor,
    data_store: ScopedPluginDataStore | None = None,
    artifact_root: Path | None = None,
) -> sdk.PluginContext:
    return sdk.PluginContext(
        identity=sdk.PluginIdentity(
            plugin_id=candidate.plugin_id,
            version=candidate.version,
            plugin_class=candidate.plugin_class,
            release_id=candidate.release_id,
            package_digest=candidate.package_digest,
            builtin_contract_hash=candidate.builtin_contract_hash,
            generation_id=generation.generation_id,
        ),
        provenance=sdk.PluginProvenanceSummary(
            source_kind=candidate.source_kind,
            public_source=candidate.public_source,
        ),
        configuration=MappingProxyType(dict(configuration)),
        data=data_store or ScopedPluginDataStore(data_root / candidate.plugin_id),
        credentials=BoundCredentialResolver(
            credential_bindings,
            credential_resolver,
            redactor,
        ),
        tasks=tasks,
        logger=RedactedPluginLogger(candidate.plugin_id, redactor),
        diagnostics=PluginDiagnosticEmitter(candidate, generation, diagnostics, redactor),
        permissions=frozenset(request.id for request in candidate.manifest.permissions),
        artifact_root=(
            str((artifact_root / candidate.plugin_id).resolve(strict=False))
            if artifact_root is not None
            else None
        ),
    )


def adapt_sdk_contributions(contributions: sdk.ContributionSet) -> PluginContributions:
    from ouro.plugins.contribution_types import contribution_type_adapters

    adapted = PluginContributions()
    for contribution_type in contribution_type_adapters():
        adapter = contribution_type.sdk
        if adapter is None:
            continue
        source = getattr(contributions, adapter.attribute)
        setattr(adapted, adapter.attribute, [adapter.adapt(item) for item in source])
    return adapted


@dataclass(frozen=True)
class SDKContributionAdapter:
    attribute: str
    adapt: Callable[[Any], Any]


def _adapt_tool(contribution: sdk.ToolContribution) -> Tool:
    async def handler(
        invocation: ToolInvocation,
        context: ToolExecutionContext,
    ) -> ToolResult | ToolInputRequest:
        call = sdk.ToolCall(
            tool_call_id=invocation.tool_call_id,
            session_id=invocation.session_id,
            run_id=invocation.run_id,
            tool_name=invocation.tool_name,
            arguments=MappingProxyType(dict(invocation.arguments)),
            workspace=str(context.workspace) if context.workspace is not None else None,
            workspace_only=context.workspace_only,
        )
        raw = _invoke_sdk_tool_handler(
            contribution.handler,
            call,
            _SDKToolExecutionContext(context),
        )
        if inspect.isawaitable(raw):
            raw = await raw
        if isinstance(raw, str):
            raw = sdk.ToolOutput(content=raw)
        if isinstance(raw, sdk.ToolInputRequest):
            return ToolInputRequest(data=raw.data)
        return ToolResult(
            tool_call_id=invocation.tool_call_id,
            tool_name=invocation.tool_name,
            content=raw.content,
            structured=dict(raw.structured),
            resource_refs=list(raw.resource_refs),
            is_error=raw.is_error,
            session_control_transitions=[
                SessionControlTransitionRequest(
                    action=item.action,
                    controller_id=item.controller_id,
                    activation_id=item.activation_id,
                    replace_activation_ids=list(item.replace_activation_ids),
                    expected_session_revision=item.expected_session_revision,
                    expected_activation_revision=item.expected_activation_revision,
                    payload=item.payload,
                    reason=item.reason,
                )
                for item in raw.session_control_transitions
            ],
        )

    async def resume_handler(
        invocation: ToolInvocation,
        resolution: ToolInputResolution,
        *,
        request: ToolInputRequest,
        context: ToolExecutionContext,
    ) -> ToolResult | ToolInputRequest:
        if contribution.resume_handler is None:
            raise RuntimeError(f"tool does not support resume: {contribution.exposed_name}")
        call = sdk.ToolResumeCall(
            tool_call_id=invocation.tool_call_id,
            session_id=invocation.session_id,
            run_id=invocation.run_id,
            tool_name=invocation.tool_name,
            arguments=MappingProxyType(dict(invocation.arguments)),
            request=request.data,
            resolution=_sdk_input_resolution(resolution),
            workspace=str(context.workspace) if context.workspace is not None else None,
            workspace_only=context.workspace_only,
        )
        raw = _invoke_sdk_tool_handler(
            contribution.resume_handler,
            call,
            _SDKToolExecutionContext(context),
        )
        if inspect.isawaitable(raw):
            raw = await raw
        if isinstance(raw, str):
            raw = sdk.ToolOutput(content=raw)
        if isinstance(raw, sdk.ToolInputRequest):
            return ToolInputRequest(data=raw.data)
        return ToolResult(
            tool_call_id=invocation.tool_call_id,
            tool_name=invocation.tool_name,
            content=raw.content,
            structured=dict(raw.structured),
            resource_refs=list(raw.resource_refs),
            is_error=raw.is_error,
            session_control_transitions=[
                SessionControlTransitionRequest(
                    action=item.action,
                    controller_id=item.controller_id,
                    activation_id=item.activation_id,
                    replace_activation_ids=list(item.replace_activation_ids),
                    expected_session_revision=item.expected_session_revision,
                    expected_activation_revision=item.expected_activation_revision,
                    payload=item.payload,
                    reason=item.reason,
                )
                for item in raw.session_control_transitions
            ],
        )

    return Tool(
        definition=ToolDefinition(
            name=contribution.exposed_name,
            description=contribution.description,
            parameters_schema=dict(contribution.parameters_schema),
            result_schema=dict(contribution.result_schema),
            permission_mode=contribution.permission_mode,
            permission_control=contribution.permission_control,
            effect=contribution.effect,
            batch_behavior=contribution.batch_behavior,
            resumable=contribution.resume_handler is not None,
            supports_unattended=contribution.supports_unattended,
            active_when_controller_id=contribution.active_when,
            metadata={"sdk_contribution_id": contribution.id},
        ),
        handler=handler,
        resume_handler=resume_handler if contribution.resume_handler is not None else None,
    )


def _sdk_input_resolution(resolution: ToolInputResolution) -> sdk.ToolInputResolution:
    if isinstance(resolution, AnswerResolution):
        return sdk.AnswerResolution(
            value=resolution.value,
            resolved_by=resolution.resolved_by,
            resolver_id=resolution.resolver_id,
        )
    if isinstance(resolution, UnattendedResolution):
        return sdk.UnattendedResolution(
            resolver_id=resolution.resolver_id,
            reason=resolution.reason,
        )
    raise TypeError(f"unsupported tool input resolution: {type(resolution).__name__}")


class _SDKToolExecutionContext:
    def __init__(self, context: ToolExecutionContext) -> None:
        self._context = context

    @property
    def workspace(self) -> str | None:
        value = self._context.workspace
        return str(value) if value is not None else None

    @property
    def workspace_only(self) -> bool:
        return self._context.workspace_only

    @property
    def is_aborted(self) -> bool:
        return self._context.is_aborted

    @property
    def abort_reason(self) -> str:
        return self._context.abort_reason

    def raise_if_aborted(self) -> None:
        self._context.raise_if_aborted()

    async def sleep(self, seconds: float) -> None:
        await self._context.sleep(seconds)

    def resolve_path(self, path: str) -> str:
        return str(self._context.resolve_path(path))

    def resolve_working_directory(self, cwd: str | None = None) -> str | None:
        resolved = self._context.resolve_working_directory(cwd)
        return str(resolved) if resolved is not None else None


def _invoke_sdk_tool_handler(
    handler,
    call: sdk.ToolCall | sdk.ToolResumeCall,
    context: sdk.ToolExecutionContext,
):
    try:
        parameters = tuple(inspect.signature(handler).parameters.values())
    except (TypeError, ValueError):
        return handler(call)
    if any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters):
        return handler(call, context=context)
    if any(parameter.name == "context" for parameter in parameters):
        return handler(call, context=context)
    positional = [
        parameter
        for parameter in parameters
        if parameter.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    if len(positional) >= 2:
        return handler(call, context)
    return handler(call)


def _adapt_hook(contribution: sdk.HookContribution) -> HookRegistration:
    async def handler(context: HookContext) -> HookResult:
        raw = contribution.handler(
            sdk.HookCall(
                event=context.hook_name,
                session_id=context.session_id,
                run_id=context.run_id,
                data=MappingProxyType(dict(context.data)),
                run_outcome=(
                    sdk.RunOutcome(
                        session_id=context.run_outcome.session_id,
                        run_id=context.run_outcome.run_id,
                        status=context.run_outcome.status,
                        origin=context.run_outcome.origin,
                        final_text=context.run_outcome.final_text,
                        error=context.run_outcome.error,
                        usage=MappingProxyType(dict(context.run_outcome.usage)),
                    )
                    if context.run_outcome is not None
                    else None
                ),
            )
        )
        if inspect.isawaitable(raw):
            raw = await raw
        if raw is None:
            return HookResult()
        return HookResult(
            diagnostics=list(raw.diagnostics),
        )

    return HookRegistration(
        plugin_id="",
        hook_name=contribution.event,
        handler=handler,
        priority=contribution.priority,
        timeout_seconds=contribution.timeout_seconds,
        hook_id=contribution.id,
        active_when_controller_id=contribution.active_when,
    )


def _adapt_command(contribution: sdk.CommandContribution) -> PluginCommand:
    async def handler(request: PluginCommandRequest) -> PluginCommandResult:
        raw = contribution.handler(
            sdk.CommandCall(
                command_id=request.command_id,
                argument=request.argument,
                session_id=request.session_id,
                workspace=request.workspace,
            )
        )
        if inspect.isawaitable(raw):
            raw = await raw
        if isinstance(raw, str):
            return PluginCommandResult(command_id=request.command_id, output=raw)
        if isinstance(raw, sdk.CommandOutput):
            return PluginCommandResult(
                command_id=request.command_id,
                output=raw.output,
                data=dict(raw.data),
                session_inputs=[
                    SessionInputRequest(prompt=item.prompt, mode=item.mode, title=item.title)
                    for item in raw.session_inputs
                ],
                session_control_transitions=[
                    SessionControlTransitionRequest(
                        action=item.action,
                        controller_id=item.controller_id,
                        activation_id=item.activation_id,
                        replace_activation_ids=list(item.replace_activation_ids),
                        expected_session_revision=item.expected_session_revision,
                        expected_activation_revision=item.expected_activation_revision,
                        payload=item.payload,
                        reason=item.reason,
                    )
                    for item in raw.session_control_transitions
                ],
            )
        return PluginCommandResult(command_id=request.command_id, data=dict(raw))

    return PluginCommand(
        name=contribution.id,
        description=contribution.description,
        usage=contribution.usage,
        handler=handler,
        timeout_seconds=contribution.timeout_seconds,
    )


def _adapt_session_controller(
    contribution: sdk.SessionControllerContribution,
) -> PluginSessionController:
    async def invoke(handler, call: PluginSessionControllerCall):
        raw = handler(
            sdk.SessionControllerCall(
                session_id=call.session_id,
                activation_id=call.activation_id,
                controller_id=call.controller_id,
                action=call.action,
                payload=call.payload,
                reason=call.reason,
                workspace=call.workspace,
            )
        )
        if inspect.isawaitable(raw):
            raw = await raw
        if not isinstance(raw, sdk.SessionControllerOutput):
            raise TypeError("session controller handler must return SessionControllerOutput")
        return PluginSessionControllerOutput(
            output=raw.output,
            data=dict(raw.data),
            owner_state_revision=raw.owner_state_revision,
        )

    return PluginSessionController(
        controller_id=contribution.id,
        description=contribution.description,
        claims=tuple(
            SessionControlClaim(domain=claim.domain, access=claim.access)
            for claim in contribution.claims
        ),
        activate_handler=lambda call: invoke(contribution.activate, call),
        deactivate_handler=lambda call: invoke(contribution.deactivate, call),
        timeout_seconds=contribution.timeout_seconds,
    )


def _adapt_context(contribution: sdk.ContextContribution) -> ContextProviderRegistration:
    async def handler(call: ContextProviderCall) -> ContextProviderResult:
        raw = contribution.handler(
            sdk.ContextCall(
                session_id=call.session_id,
                run_id=call.run_id,
                message_count=call.message_count,
                model_round=call.model_round,
            )
        )
        if inspect.isawaitable(raw):
            raw = await raw
        if isinstance(raw, sdk.ContextOutput):
            values = raw.fragments
        elif isinstance(raw, str):
            values = (raw,)
        else:
            values = raw
        return ContextProviderResult(
            fragments=[
                ContextFragment(text=value.text, persistence=value.persistence)
                if isinstance(value, sdk.ContextFragment)
                else ContextFragment(text=value)
                for value in values
                if value
            ]
        )

    return ContextProviderRegistration(
        plugin_id="",
        provider_id=contribution.id,
        handler=handler,
        priority=contribution.priority,
        timeout_seconds=contribution.timeout_seconds,
        failure_mode=contribution.failure_mode,
        active_when_controller_id=contribution.active_when,
    )


class _SDKCompactionStrategy:
    def __init__(self, contribution: sdk.CompactionContribution) -> None:
        self.strategy_id = contribution.id
        self.version = contribution.version
        self._handler = contribution.handler

    async def compact(self, request: CompactionRequest, *, signal=None) -> CompactionArtifact:
        del signal
        raw = self._handler(
            sdk.CompactionCall(
                session_id=request.session_id,
                run_id=request.run_id,
                previous_summary=request.previous_summary,
                messages=tuple(
                    message.model_dump(mode="json", exclude_none=True)
                    for message in request.messages
                ),
                input_token_budget=request.input_token_budget,
                summary_output_tokens=request.summary_output_tokens,
            )
        )
        if inspect.isawaitable(raw):
            raw = await raw
        return CompactionArtifact(summary=raw.summary, usage=dict(raw.usage))


class _SDKBackgroundService:
    def __init__(self, contribution: sdk.BackgroundServiceContribution) -> None:
        self.service_id = contribution.id
        self._start = contribution.start
        self._stop = contribution.stop

    async def start(self) -> None:
        result = self._start()
        if inspect.isawaitable(result):
            await result

    async def stop(self) -> None:
        result = self._stop()
        if inspect.isawaitable(result):
            await result


def _adapt_continuation_policy(
    contribution: sdk.ContinuationPolicyContribution,
) -> ContinuationPolicyRegistration:
    async def handler(outcome) -> ContinuationDecision:
        sdk_outcome = sdk.RunOutcome(
            session_id=outcome.session_id,
            run_id=outcome.run_id,
            status=outcome.status,
            origin=outcome.origin,
            final_text=outcome.final_text,
            error=outcome.error,
            usage=MappingProxyType(dict(outcome.usage)),
        )
        raw = contribution.handler(sdk_outcome)
        if inspect.isawaitable(raw):
            raw = await raw
        if not isinstance(raw, sdk.ContinuationDecision):
            raise TypeError("continuation policy must return ContinuationDecision")
        return ContinuationDecision(
            action=raw.action,
            prompt=raw.prompt,
            mode=raw.mode,
            title=raw.title,
            reason=raw.reason,
            state_revision=raw.state_revision,
        )

    return ContinuationPolicyRegistration(
        plugin_id="",
        policy_id=contribution.id,
        handler=handler,
        priority=contribution.priority,
        timeout_seconds=contribution.timeout_seconds,
        active_when_controller_id=contribution.active_when,
    )


def _adapt_tool_policy(
    contribution: sdk.ToolPolicyContribution,
) -> ToolPolicyRegistration:
    async def handler(call: ToolPolicyCall) -> ToolPolicyDecision:
        raw = contribution.handler(
            sdk.ToolPolicyCall(
                session_id=call.session_id,
                run_id=call.run_id,
                tool_call_id=call.tool_call_id,
                tool_name=call.tool_name,
                canonical_tool_id=call.canonical_tool_id,
                owner_id=call.owner_id,
                arguments=MappingProxyType(dict(call.arguments)),
                source=call.source,
                effect=call.effect,
                batch_behavior=call.batch_behavior,
                permission_control=call.permission_control,
                workspace=call.workspace,
            )
        )
        if inspect.isawaitable(raw):
            raw = await raw
        if not isinstance(raw, sdk.ToolPolicyDecision):
            raise TypeError("tool policy must return ToolPolicyDecision")
        return ToolPolicyDecision(action=raw.action, reason=raw.reason)

    return ToolPolicyRegistration(
        plugin_id="",
        policy_id=contribution.id,
        handler=handler,
        priority=contribution.priority,
        timeout_seconds=contribution.timeout_seconds,
        active_when_controller_id=contribution.active_when,
    )


def _adapt_turn_policy(
    contribution: sdk.TurnPolicyContribution,
) -> TurnPolicyRegistration:
    async def handler(candidate: TurnCompletionCandidate) -> TurnPolicyDecision:
        raw = contribution.handler(
            sdk.TurnCompletionCall(
                session_id=candidate.session_id,
                run_id=candidate.run_id,
                turn_id=candidate.turn_id,
                assistant_text=candidate.assistant_text,
                assistant_reasoning=candidate.assistant_reasoning,
            )
        )
        if inspect.isawaitable(raw):
            raw = await raw
        if not isinstance(raw, sdk.TurnPolicyDecision):
            raise TypeError("turn policy must return TurnPolicyDecision")
        return TurnPolicyDecision(action=raw.action, feedback=raw.feedback)

    return TurnPolicyRegistration(
        plugin_id="",
        policy_id=contribution.id,
        handler=handler,
        priority=contribution.priority,
        timeout_seconds=contribution.timeout_seconds,
        active_when_controller_id=contribution.active_when,
    )


TOOL_SDK_ADAPTER = SDKContributionAdapter("tools", _adapt_tool)
HOOK_SDK_ADAPTER = SDKContributionAdapter("hooks", _adapt_hook)
COMMAND_SDK_ADAPTER = SDKContributionAdapter("commands", _adapt_command)
CONTEXT_SDK_ADAPTER = SDKContributionAdapter("context_providers", _adapt_context)
COMPACTION_SDK_ADAPTER = SDKContributionAdapter(
    "compaction_strategies",
    _SDKCompactionStrategy,
)
BACKGROUND_SERVICE_SDK_ADAPTER = SDKContributionAdapter(
    "background_services",
    _SDKBackgroundService,
)
CONTINUATION_SDK_ADAPTER = SDKContributionAdapter(
    "continuation_policies",
    _adapt_continuation_policy,
)
TOOL_POLICY_SDK_ADAPTER = SDKContributionAdapter("tool_policies", _adapt_tool_policy)
TURN_POLICY_SDK_ADAPTER = SDKContributionAdapter("turn_policies", _adapt_turn_policy)
SESSION_CONTROLLER_SDK_ADAPTER = SDKContributionAdapter(
    "session_controllers",
    _adapt_session_controller,
)


def _module_belongs_to(module: ModuleType, package_root: Path) -> bool:
    filename = getattr(module, "__file__", None)
    if filename is None:
        return False
    try:
        return Path(filename).resolve().is_relative_to(package_root.resolve())
    except OSError:
        return False
