from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
import os
import shutil
import sys
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from types import MappingProxyType, ModuleType
from uuid import uuid4

from ouro.core.compaction import CompactionArtifact, CompactionRequest
from ouro.core.continuations import (
    ContinuationDecision,
    ContinuationPolicyRegistration,
)
from ouro.core.hooks import HookContext, HookRegistration, HookResult
from ouro.core.redaction import SensitiveValueRedactor
from ouro.core.session_controls import SessionInputRequest
from ouro.core.tool_policies import (
    ToolPolicyCall,
    ToolPolicyDecision,
    ToolPolicyRegistration,
)
from ouro.plugins.contributions import PluginContributions
from ouro.plugins.models import InstalledPlugin, PluginDiagnostic, PluginGenerationRecord
from ouro.plugins.paths import portable_relative_path
from ouro.plugins.registry import (
    PluginCommand,
    PluginCommandRequest,
    PluginCommandResult,
    PluginContextProvider,
)
from ouro.sdk.plugins import v1 as sdk
from ouro.tools import Tool, ToolDefinition, ToolExecutionContext, ToolInvocation, ToolResult


class ScopedPluginDataStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def read_bytes(self, relative_path: str) -> bytes:
        return self._path(relative_path).read_bytes()

    def write_bytes(self, relative_path: str, value: bytes) -> None:
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

    def delete(self, relative_path: str) -> None:
        path = self._path(relative_path)
        if path.exists():
            path.unlink()

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
    def __init__(self, on_failure: Callable[[Exception], None] | None = None) -> None:
        self._tasks: set[asyncio.Task[object]] = set()
        self._closed = False
        self._on_failure = on_failure

    def start(self, coroutine: Awaitable[object], *, name: str | None = None) -> None:
        if self._closed:
            if inspect.iscoroutine(coroutine):
                coroutine.close()
            raise RuntimeError("plugin task group is closed")

        async def run() -> object:
            return await coroutine

        task: asyncio.Task[object] = asyncio.create_task(run(), name=name)
        self._tasks.add(task)
        task.add_done_callback(self._task_finished)

    def _task_finished(self, task: asyncio.Task[object]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        exception = task.exception()
        if isinstance(exception, Exception) and self._on_failure is not None:
            self._on_failure(exception)

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
        installed: InstalledPlugin,
        generation: PluginGenerationRecord,
        sink: list[PluginDiagnostic],
        redactor: SensitiveValueRedactor,
    ) -> None:
        self.installed = installed
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
                plugin_id=self.installed.plugin_id,
                version=self.installed.version,
                package_digest=self.installed.package_digest,
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
) -> tuple[Callable[[sdk.PluginContext], object], LoadedPluginModule]:
    module_name, _, factory_name = entrypoint.partition(":")
    top_level = module_name.partition(".")[0]
    existing = sys.modules.get(top_level)
    if existing is not None and not _module_belongs_to(existing, package_root):
        raise RuntimeError(f"plugin top-level module conflicts with loaded module: {top_level}")
    before = set(sys.modules)
    sys.path.insert(0, str(package_root))
    previous_bytecode_setting = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        try:
            module = importlib.import_module(module_name)
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
        with suppress(ValueError):
            sys.path.remove(str(package_root))
    imported = {
        name
        for name in set(sys.modules) - before | {module_name}
        if (loaded := sys.modules.get(name)) is not None
        and _module_belongs_to(loaded, package_root)
    }
    return factory, LoadedPluginModule(module, package_root, imported)


def build_plugin_context(
    installed: InstalledPlugin,
    generation: PluginGenerationRecord,
    *,
    data_root: Path,
    configuration: dict[str, object],
    credential_bindings: dict[str, str],
    credential_resolver: Callable[[str], str] | None,
    tasks: OwnedTaskGroup,
    diagnostics: list[PluginDiagnostic],
    redactor: SensitiveValueRedactor,
) -> sdk.PluginContext:
    return sdk.PluginContext(
        identity=sdk.PluginIdentity(
            plugin_id=installed.plugin_id,
            version=installed.version,
            package_digest=installed.package_digest,
            generation_id=generation.generation_id,
        ),
        provenance=sdk.PluginProvenanceSummary(
            source_kind=installed.provenance.source_kind,
            public_source=installed.provenance.public_source,
        ),
        configuration=MappingProxyType(dict(configuration)),
        data=ScopedPluginDataStore(data_root / installed.plugin_id),
        credentials=BoundCredentialResolver(
            credential_bindings,
            credential_resolver,
            redactor,
        ),
        tasks=tasks,
        logger=RedactedPluginLogger(installed.plugin_id, redactor),
        diagnostics=PluginDiagnosticEmitter(installed, generation, diagnostics, redactor),
        permissions=frozenset(request.id for request in installed.manifest.permissions),
    )


def adapt_sdk_contributions(contributions: sdk.ContributionSet) -> PluginContributions:
    return PluginContributions(
        tools=[_adapt_tool(item) for item in contributions.tools],
        hooks=[_adapt_hook(item) for item in contributions.hooks],
        commands=[_adapt_command(item) for item in contributions.commands],
        context_providers=[_adapt_context(item) for item in contributions.context_providers],
        compaction_strategies=[
            _SDKCompactionStrategy(item) for item in contributions.compaction_strategies
        ],
        background_services=[
            _SDKBackgroundService(item) for item in contributions.background_services
        ],
        continuation_policies=[
            _adapt_continuation_policy(item) for item in contributions.continuation_policies
        ],
        tool_policies=[_adapt_tool_policy(item) for item in contributions.tool_policies],
    )


def _adapt_tool(contribution: sdk.ToolContribution) -> Tool:
    async def handler(
        invocation: ToolInvocation,
        context: ToolExecutionContext,
    ) -> ToolResult:
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
        return ToolResult(
            tool_call_id=invocation.tool_call_id,
            tool_name=invocation.tool_name,
            content=raw.content,
            structured=dict(raw.structured),
            resource_refs=list(raw.resource_refs),
            is_error=raw.is_error,
        )

    return Tool(
        definition=ToolDefinition(
            name=contribution.exposed_name,
            description=contribution.description,
            parameters_schema=dict(contribution.parameters_schema),
            result_schema=dict(contribution.result_schema),
            permission_mode=contribution.permission_mode,
            metadata={"sdk_contribution_id": contribution.id},
        ),
        handler=handler,
    )


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
    call: sdk.ToolCall,
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
            added_context=list(raw.added_context),
            diagnostics=list(raw.diagnostics),
        )

    return HookRegistration(
        plugin_id="",
        hook_name=contribution.event,
        handler=handler,
        priority=contribution.priority,
        timeout_seconds=contribution.timeout_seconds,
        hook_id=contribution.id,
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
            )
        return PluginCommandResult(command_id=request.command_id, data=dict(raw))

    return PluginCommand(
        name=contribution.id,
        description=contribution.description,
        handler=handler,
        timeout_seconds=contribution.timeout_seconds,
    )


def _adapt_context(contribution: sdk.ContextContribution) -> PluginContextProvider:
    hook = _adapt_hook(
        sdk.HookContribution(
            id=contribution.id,
            event="after_context_build",
            handler=contribution.handler,
            priority=contribution.priority,
        )
    )
    return PluginContextProvider(
        provider_id=contribution.id,
        handler=hook.handler,
        priority=contribution.priority,
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
                arguments=MappingProxyType(dict(call.arguments)),
                source=call.source,
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
    )


def _module_belongs_to(module: ModuleType, package_root: Path) -> bool:
    filename = getattr(module, "__file__", None)
    if filename is None:
        return False
    try:
        return Path(filename).resolve().is_relative_to(package_root.resolve())
    except OSError:
        return False
