from __future__ import annotations

import inspect
from collections.abc import Mapping
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from ouro.application.compaction import (
    ContextPreparationService,
    LLMSummaryCompactionStrategy,
)
from ouro.application.events import RuntimeEventRecorder, RuntimeEventSink
from ouro.application.followups import FollowUpQueue
from ouro.application.models import (
    AvailableModel,
    FixedRuntimeAdapterCatalog,
    RuntimeAdapterCatalog,
)
from ouro.application.queued_inputs import QueuedInput, QueuedInputMode, QueuedInputQueue
from ouro.application.recovery import RunRecoveryService
from ouro.application.runs import (
    ACTIVE_RUN_STATUSES,
    RunLifecycle,
    clear_pending_metadata,
    find_active_run,
    find_run,
    maybe_active_run,
)
from ouro.application.sessions import SessionService
from ouro.application.tool_calls import ToolCallCoordinator
from ouro.application.usage import UsageService
from ouro.config.compaction import CompactionConfig
from ouro.config.queued_inputs import QueuedInputConfig
from ouro.config.tool_execution import ToolExecutionConfig
from ouro.core.adapter import RuntimeAdapter
from ouro.core.approvals import SESSION_APPROVAL_MODES, SessionApprovalMode
from ouro.core.base import new_id, utc_now
from ouro.core.cancellation import AbortSignal
from ouro.core.compaction import CompactionCommandResult, CompactionStrategyRegistry
from ouro.core.context import ContextBuilder
from ouro.core.errors import CompactionError, ConfigError, ResourceAttachmentError, SessionBusyError
from ouro.core.events import EventRecord
from ouro.core.hooks import HookRegistry
from ouro.core.inner_loop import InnerAgentLoop, InnerLoopPorts, TurnBoundaryInputResult
from ouro.core.messages import MessageRecord, ProviderContinuationArtifact
from ouro.core.results import RunResult
from ouro.core.run_budget import RunBudgetPolicy
from ouro.core.state import RunRecord, RunStatus, SessionRecord
from ouro.core.tool_batches import ToolCallBatchRecord
from ouro.core.tools import PermissionMode
from ouro.prompts import DEFAULT_SYSTEM_PROMPT
from ouro.sessions import SessionLock, SessionSnapshot, SessionStore, session_mutation
from ouro.tools import ToolExecutor, ToolRegistry, ToolResult

if TYPE_CHECKING:
    from ouro.application.resources import ResourceService


class RuntimeService:
    def __init__(
        self,
        *,
        store: SessionStore,
        adapter: RuntimeAdapter,
        model_catalog: RuntimeAdapterCatalog | None = None,
        tools: ToolRegistry,
        hooks: HookRegistry | None = None,
        context_builder: ContextBuilder | None = None,
        run_budget: RunBudgetPolicy | None = None,
        system_prompt: str | None = None,
        event_sink: RuntimeEventSink | None = None,
        permission_overrides: Mapping[str, PermissionMode] | None = None,
        tool_execution_config: ToolExecutionConfig | None = None,
        compaction_config: CompactionConfig | None = None,
        compaction_strategies: CompactionStrategyRegistry | None = None,
        compaction_adapter: RuntimeAdapter | None = None,
        compaction_adapter_error: str | None = None,
        queued_input_config: QueuedInputConfig | None = None,
        resources: ResourceService | None = None,
        usage_service: UsageService | None = None,
        default_workspace: str | Path | None = None,
    ) -> None:
        self.store = store
        self.resources = resources
        self.usage = usage_service
        self.sessions = SessionService(store, resources=resources)
        self.queued_inputs = QueuedInputQueue(store)
        self.follow_ups = FollowUpQueue(store)
        self.queued_input_config = queued_input_config or QueuedInputConfig()
        self.adapter = adapter
        self.models = model_catalog or FixedRuntimeAdapterCatalog(adapter)
        self.tools = tools
        self.default_workspace = (
            str(Path(default_workspace).expanduser().resolve())
            if default_workspace is not None
            else None
        )
        self.tool_executor = ToolExecutor(tools, permission_overrides=permission_overrides)
        self.hooks = hooks or HookRegistry()
        self.event_recorder = RuntimeEventRecorder(
            store=store,
            hooks=self.hooks,
            event_sink=event_sink,
        )
        self.run_lifecycle = RunLifecycle(
            store=store,
            events=self.event_recorder,
            usage_service=usage_service,
            validate_resource_refs=(
                resources.validate_references if resources is not None else None
            ),
        )
        self.run_recovery = RunRecoveryService(
            store=store,
            runs=self.run_lifecycle,
            events=self.event_recorder,
        )
        self.tool_calls = ToolCallCoordinator(
            executor=self.tool_executor,
            registry=tools,
            hooks=self.hooks,
            events=self.event_recorder,
            runs=self.run_lifecycle,
            execution_config=tool_execution_config or ToolExecutionConfig(),
            default_workspace=self.default_workspace,
            is_aborted=self._is_run_aborted,
        )
        self.context_builder = context_builder or ContextBuilder()
        self.run_budget = run_budget or RunBudgetPolicy()
        self.system_prompt = DEFAULT_SYSTEM_PROMPT if system_prompt is None else system_prompt
        self.compaction_config = compaction_config or CompactionConfig()
        self.compaction_strategies = compaction_strategies or CompactionStrategyRegistry()
        configured_adapter_error = compaction_adapter_error
        if (
            self.compaction_config.model.has_dedicated_endpoint
            and compaction_adapter is None
            and configured_adapter_error is None
        ):
            configured_adapter_error = (
                "configured endpoint unavailable: no compaction adapter was supplied"
            )
        try:
            self.compaction_strategies.get(LLMSummaryCompactionStrategy.strategy_id)
        except KeyError:
            self.compaction_strategies.register(
                LLMSummaryCompactionStrategy(
                    adapter,
                    configured_adapter=compaction_adapter,
                    configured_adapter_error=configured_adapter_error,
                    model_config=self.compaction_config.model,
                    runtime_adapter_resolver=self._adapter_for_session_id,
                    usage_recorder=self.run_lifecycle.record_usage_for_run,
                )
            )
        self.context_preparation = ContextPreparationService(
            store=store,
            adapter=adapter,
            hooks=self.hooks,
            events=self.event_recorder,
            context_builder=self.context_builder,
            strategies=self.compaction_strategies,
            config=self.compaction_config,
            system_prompt=self.system_prompt,
            project_model_messages=(
                resources.project_model_messages if resources is not None else None
            ),
        )
        self._abort_signals: dict[str, AbortSignal] = {}

    def add_event_sink(self, event_sink: RuntimeEventSink) -> None:
        self.event_recorder.add_event_sink(event_sink)

    async def close(self) -> None:
        close = getattr(self.models, "close", None)
        if close is not None:
            result = close()
            if inspect.isawaitable(result):
                await result

    def available_models(self) -> list[AvailableModel]:
        """Return enabled model endpoints from the current system configuration."""

        return self.models.list_models()

    def current_model(self, session_id: str | None = None) -> AvailableModel:
        if session_id is None:
            return self.models.default_model()
        session = self.store.load_session(session_id, recover=False).session
        return self.models.resolve(session.current_model or self.models.default_model().key)

    def switch_session_model(self, session_id: str, selector: str) -> SessionRecord:
        """Persist an idle session's model route without changing other sessions."""

        lock = self._acquire_session_run_lock(session_id, new_id("model"))
        if lock is None:
            raise SessionBusyError(f"session already has an active run: {session_id}")
        try:
            snapshot = self.store.load_session(session_id, recover=False)
            if maybe_active_run(snapshot.runs) is not None:
                raise SessionBusyError(f"session already has an active run: {session_id}")
            normalized = selector.strip()
            model = next(
                (item for item in self.models.list_models() if item.key == normalized),
                None,
            )
            if model is None:
                raise ConfigError(f"model not found: {selector}")
            self.models.adapter_for(model.key)
            session = snapshot.session.model_copy(
                update={
                    "current_model": model.key,
                    "updated_at": utc_now(),
                    "last_activity_at": utc_now(),
                }
            )
            self.store.save_session(session)
            self.store.try_update_index(session.session_id)
            return session
        finally:
            self._release_session_run_lock(lock)

    def set_session_approval_mode(
        self,
        session_id: str,
        approval_mode: SessionApprovalMode,
    ) -> SessionRecord:
        """Persist the approval policy used by future tool calls in a session."""

        self.sessions.set_approval_mode(session_id, approval_mode)
        return self.store.load_session(session_id, recover=False).session

    def _resolve_session_adapter(
        self,
        session: SessionRecord,
    ) -> tuple[SessionRecord, RuntimeAdapter]:
        selection = self.models.resolve(session.current_model or self.models.default_model().key)
        adapter = self.models.adapter_for(selection.key)
        if session.current_model == selection.key:
            return session, adapter
        session = session.model_copy(
            update={
                "current_model": selection.key,
                "updated_at": utc_now(),
                "last_activity_at": utc_now(),
            }
        )
        self.store.save_session(session)
        self.store.try_update_index(session.session_id)
        return session, adapter

    def _adapter_for_session_id(self, session_id: str) -> RuntimeAdapter:
        session = self.store.load_session(session_id, recover=False).session
        _, adapter = self._resolve_session_adapter(session)
        return adapter

    async def run(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        title: str | None = None,
        queue_mode: QueuedInputMode = "follow_up",
        approval_mode: SessionApprovalMode | None = None,
        resource_refs: list[str] | tuple[str, ...] | None = None,
        _drain_queued_inputs: bool = True,
        _queued_inputs: list[QueuedInput] | None = None,
    ) -> RunResult:
        if approval_mode is not None and approval_mode not in SESSION_APPROVAL_MODES:
            raise ValueError(f"invalid session approval mode: {approval_mode}")
        input_resource_refs = tuple(dict.fromkeys(resource_refs or ()))
        if input_resource_refs and session_id is None:
            raise ResourceAttachmentError(
                "resource attachments require an existing session; import the resource "
                "and pass its session with session_id"
            )
        if session_id is None:
            created = self.sessions.create(
                title=title or _title_from_prompt(prompt),
                current_model=self.models.default_model().key,
                approval_mode=approval_mode or "inherit",
                workspace=self.default_workspace,
            )
            session = self.store.load_session(
                created.session.session_id,
                recover=False,
            ).session
            existing_messages: list[MessageRecord] = []
            existing_events: list[EventRecord] = []
        else:
            snapshot = self.store.load_session(
                session_id,
                recover=not self._is_session_run_locked(session_id),
            )
            session = snapshot.session
            existing_messages = snapshot.messages
            existing_events = snapshot.events
            active_run = maybe_active_run(snapshot.runs)
            self._validate_model_resource_refs(session, input_resource_refs)
            if active_run is not None:
                if approval_mode is not None and approval_mode != session.approval_mode:
                    raise SessionBusyError(
                        "cannot change approval mode while the session has an active run: "
                        f"{session_id}"
                    )
                queued_session, queued_events = await self._queue_input(
                    snapshot=snapshot,
                    active_run=active_run,
                    prompt=prompt,
                    title=title,
                    mode=queue_mode,
                    resource_refs=input_resource_refs,
                )
                return RunResult(session=queued_session, run=active_run, events=queued_events)
        sequence = len(existing_events)
        emitted: list[EventRecord] = []

        run = RunRecord(
            session_id=session.session_id,
            status="running",
            origin="follow_up" if _queued_inputs else "user",
        )
        run_lock = self._acquire_session_run_lock(session.session_id, run.run_id)
        if run_lock is None:
            snapshot = self.store.load_session(session.session_id, recover=False)
            active_run = maybe_active_run(snapshot.runs)
            self._validate_model_resource_refs(snapshot.session, input_resource_refs)
            if active_run is not None:
                if approval_mode is not None and approval_mode != snapshot.session.approval_mode:
                    raise SessionBusyError(
                        "cannot change approval mode while the session has an active run: "
                        f"{session.session_id}"
                    )
                queued_session, queued_events = await self._queue_input(
                    snapshot=snapshot,
                    active_run=active_run,
                    prompt=prompt,
                    title=title,
                    mode=queue_mode,
                    resource_refs=input_resource_refs,
                )
                return RunResult(session=queued_session, run=active_run, events=queued_events)
            raise SessionBusyError(f"session is already locked: {session.session_id}")
        abort_signal: AbortSignal | None = None
        try:
            locked_snapshot = self.store.load_session(session.session_id, recover=False)
            locked_snapshot = await self.run_recovery.reconcile(locked_snapshot)
            session = locked_snapshot.session
            locked_active_run = maybe_active_run(locked_snapshot.runs)
            if locked_active_run is not None:
                if approval_mode is not None and approval_mode != session.approval_mode:
                    raise SessionBusyError(
                        "cannot change approval mode while the session has an active run: "
                        f"{session.session_id}"
                    )
                queued_session, queued_events = await self._queue_input(
                    snapshot=locked_snapshot,
                    active_run=locked_active_run,
                    prompt=prompt,
                    title=title,
                    mode=queue_mode,
                    resource_refs=input_resource_refs,
                )
                return RunResult(
                    session=queued_session,
                    run=locked_active_run,
                    events=queued_events,
                )
            if approval_mode is not None and approval_mode != session.approval_mode:
                now = utc_now()
                session = session.model_copy(
                    update={
                        "approval_mode": approval_mode,
                        "updated_at": now,
                        "last_activity_at": now,
                    }
                )
                self.store.save_session(session)
                self.store.try_update_index(session.session_id)
            session, active_adapter = self._resolve_session_adapter(session)
            existing_messages = locked_snapshot.messages
            existing_events = locked_snapshot.events
            sequence = len(existing_events)
            pending_inputs = _queued_inputs or [
                QueuedInput(
                    prompt=prompt,
                    title=title,
                    mode=queue_mode,
                    resource_refs=input_resource_refs,
                )
            ]
            for pending_input in pending_inputs:
                self._validate_model_resource_refs(
                    session,
                    pending_input.resource_refs,
                    adapter=active_adapter,
                )
            run = run.model_copy(update={"model": active_adapter.model.model_id})
            session, run = self.run_lifecycle.start(session=session, run=run)

            sequence = await self.event_recorder.emit(
                emitted,
                sequence,
                session_id=session.session_id,
                run_id=run.run_id,
                event_type="run_started",
                payload={"trigger": "user"},
            )

            user_messages: list[MessageRecord] = []
            for queued_input in pending_inputs:
                is_queued = _queued_inputs is not None
                user_message = self.run_lifecycle.append_user_message(
                    session=session,
                    run=run,
                    prompt=queued_input.prompt,
                    sequence=len(existing_messages) + len(user_messages) + 1,
                    resource_refs=queued_input.resource_refs,
                    metadata=(
                        {
                            "queued_input_id": queued_input.queue_id,
                            "queued_input_mode": queued_input.mode,
                        }
                        if is_queued
                        else None
                    ),
                )
                user_messages.append(user_message)
                if is_queued:
                    latest = self.store.load_session(session.session_id, recover=False)
                    self.queued_inputs.acknowledge(latest, queued_input.queue_id)
                sequence = await self.event_recorder.emit(
                    emitted,
                    sequence,
                    session_id=session.session_id,
                    run_id=run.run_id,
                    event_type="user_message_added",
                    payload={
                        "message_id": user_message.message_id,
                        "queued_input_id": queued_input.queue_id if is_queued else None,
                        "resource_refs": list(queued_input.resource_refs),
                    },
                )

            abort_signal = self._activate_abort_signal(session.session_id, run.run_id)
            result = await self._continue_model_loop(
                session=session,
                run=run,
                messages=[*existing_messages, *user_messages],
                sequence=sequence,
                emitted=emitted,
                abort_signal=abort_signal,
                adapter=active_adapter,
            )
        finally:
            if abort_signal is not None:
                self._release_abort_signal(run.run_id, abort_signal)
            self._release_session_run_lock(run_lock)
        self._finish_run_hooks(result.run)
        if _drain_queued_inputs and result.run.status == "completed":
            await self._drain_queued_input_queue(result.session.session_id)
        return result

    async def _continue_model_loop(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        messages: list[MessageRecord],
        sequence: int,
        emitted: list[EventRecord],
        final_parts: list[str] | None = None,
        tool_results: list[ToolResult] | None = None,
        abort_signal: AbortSignal,
        adapter: RuntimeAdapter,
    ) -> RunResult:
        loop = InnerAgentLoop(
            adapter=adapter,
            tools=self.tools,
            run_budget=self.run_budget,
            ports=InnerLoopPorts(
                prepare_context=partial(self.context_preparation.prepare, adapter=adapter),
                emit=self.event_recorder.emit,
                record_hook_failures=self.event_recorder.record_hook_failures,
                handle_tool_batch=self.tool_calls.handle_batch,
                record_usage=self.run_lifecycle.record_usage,
                aborted_result=self._aborted_result,
                save_waiting_state=self.run_lifecycle.save_waiting_state,
                complete_run=self.run_lifecycle.complete,
                fail_run=self.run_lifecycle.fail,
                pause_run=self.run_lifecycle.pause,
                consume_turn_boundary_inputs=self._consume_turn_boundary_inputs,
            ),
        )
        return await loop.run(
            session=session,
            run=run,
            messages=messages,
            sequence=sequence,
            emitted=emitted,
            final_parts=final_parts,
            tool_results=tool_results,
            abort_signal=abort_signal,
        )

    async def compact(self, *, session_id: str) -> CompactionCommandResult:
        """Manually compact a session through the same durable summary pipeline as auto mode."""

        snapshot = self.store.load_session(session_id)
        if maybe_active_run(snapshot.runs) is not None:
            raise SessionBusyError(f"session already has an active run: {session_id}")
        run = RunRecord(
            session_id=session_id,
            kind="compaction",
            trigger="manual_compaction",
            status="running",
        )
        run_lock = self._acquire_session_run_lock(session_id, run.run_id)
        if run_lock is None:
            raise SessionBusyError(f"session is already locked: {session_id}")
        emitted: list[EventRecord] = []
        sequence = len(snapshot.events)
        abort_signal = self._activate_abort_signal(session_id, run.run_id)
        terminal_run = run
        try:
            snapshot = self.store.load_session(session_id, recover=False)
            if maybe_active_run(snapshot.runs) is not None:
                raise SessionBusyError(f"session already has an active run: {session_id}")
            active_session, active_adapter = self._resolve_session_adapter(snapshot.session)
            run = run.model_copy(update={"model": active_adapter.model.model_id})
            terminal_run = run
            active_session, run = self.run_lifecycle.start(session=active_session, run=run)
            sequence = await self.event_recorder.emit(
                emitted,
                sequence,
                session_id=session_id,
                run_id=run.run_id,
                event_type="run_started",
                payload={"trigger": "manual_compaction", "kind": "compaction"},
            )
            try:
                execution = await self.context_preparation.compact(
                    emitted,
                    sequence,
                    session=active_session,
                    run=run,
                    messages=snapshot.messages,
                    tools=self.tools.definitions(),
                    added_context=[],
                    trigger="manual",
                    force=True,
                    abort_signal=abort_signal,
                    adapter=active_adapter,
                )
            except Exception as exc:
                failed = await self.run_lifecycle.fail(
                    emitted,
                    len(self.store.load_session(session_id, recover=False).events),
                    session=active_session,
                    run=run,
                    error=str(exc),
                    final_text="",
                    tool_results=[],
                )
                terminal_run = failed.run
                idle_session = failed.session.model_copy(
                    update={
                        "status": "idle",
                        "active_run_id": None,
                        "updated_at": utc_now(),
                        "last_activity_at": utc_now(),
                    }
                )
                self.store.save_session(idle_session)
                self.store.try_update_index(idle_session.session_id)
                if isinstance(exc, CompactionError):
                    raise
                raise CompactionError(f"manual compaction failed: {exc}") from exc
            completed = await self.run_lifecycle.complete(
                emitted,
                execution.sequence,
                session=active_session,
                run=run,
                messages=snapshot.messages,
                final_text="",
                tool_results=[],
            )
            terminal_run = completed.run
            return CompactionCommandResult(
                session=completed.session,
                run=completed.run,
                compaction=execution.record,
                events=emitted,
                skipped=execution.skipped,
            )
        finally:
            self._release_abort_signal(run.run_id, abort_signal)
            self._release_session_run_lock(run_lock)
            self._finish_run_hooks(terminal_run)

    async def abort_run(
        self,
        *,
        session_id: str,
        run_id: str | None = None,
        reason: str = "User aborted",
    ) -> RunResult:
        snapshot = self.store.load_session(session_id, recover=False)
        run = find_run(snapshot.runs, run_id) if run_id else find_active_run(snapshot.runs)
        if run.status not in ACTIVE_RUN_STATUSES:
            return RunResult(session=snapshot.session, run=run)

        emitted: list[EventRecord] = []
        signal = self._abort_signals.get(run.run_id)
        if signal is not None:
            signal.abort(reason)
        with session_mutation(self.store, session_id):
            snapshot = self.store.load_session(session_id, recover=False)
            run = find_run(snapshot.runs, run.run_id)
            if run.status not in ACTIVE_RUN_STATUSES:
                return RunResult(session=snapshot.session, run=run)
            metadata = clear_pending_metadata(run.metadata)
            metadata["abort_reason"] = reason
            run = run.model_copy(
                update={
                    "status": "aborted",
                    "ended_at": utc_now(),
                    "pending_approval_id": None,
                    "abort_requested": True,
                    "error": reason,
                    "metadata": metadata,
                }
            )
            session = snapshot.session.model_copy(
                update={
                    "status": "idle",
                    "active_run_id": None,
                    "updated_at": utc_now(),
                    "last_activity_at": utc_now(),
                    "last_run_id": run.run_id,
                }
            )
            self.store.save_run(run)
            self.store.save_session(session)
        self.store.try_update_index(session.session_id)
        await self.event_recorder.emit(
            emitted,
            len(snapshot.events),
            session_id=session.session_id,
            run_id=run.run_id,
            event_type="run_aborted",
            payload={"reason": reason},
        )
        self._finish_run_hooks(run)
        return RunResult(session=session, run=run, events=emitted)

    async def _queue_input(
        self,
        *,
        snapshot: SessionSnapshot,
        active_run: RunRecord,
        prompt: str,
        title: str | None,
        mode: QueuedInputMode,
        resource_refs: tuple[str, ...] = (),
    ) -> tuple[SessionRecord, list[EventRecord]]:
        session, queued_input, queue_size = self.queued_inputs.enqueue(
            snapshot=snapshot,
            prompt=prompt,
            title=title,
            mode=mode,
            resource_refs=resource_refs,
        )
        emitted: list[EventRecord] = []
        sequence = await self.event_recorder.emit(
            emitted,
            len(snapshot.events),
            session_id=session.session_id,
            run_id=active_run.run_id,
            event_type=(
                "follow_up_input_queued"
                if queued_input.mode == "follow_up"
                else "steering_input_queued"
            ),
            payload={
                "queue_id": queued_input.queue_id,
                "mode": queued_input.mode,
                "queue_size": queue_size,
                "resource_refs": list(queued_input.resource_refs),
            },
        )
        del sequence
        return session, emitted

    async def _drain_queued_input_queue(self, session_id: str) -> None:
        while True:
            snapshot = self.store.load_session(
                session_id,
                recover=not self._is_session_run_locked(session_id),
            )
            if maybe_active_run(snapshot.runs) is not None:
                return
            limit = 1 if self.queued_input_config.consumption_mode == "one_by_one" else None
            queued = self.queued_inputs.eligible(snapshot, limit=limit)
            if not queued:
                return
            pending = [
                item
                for item in queued
                if not self._queued_input_is_durable(snapshot.messages, item.queue_id)
            ]
            for item in queued:
                if item not in pending:
                    self.queued_inputs.acknowledge(snapshot, item.queue_id)
            if not pending:
                continue
            result = await self.run(
                pending[0].prompt,
                session_id=session_id,
                title=pending[0].title,
                _drain_queued_inputs=False,
                _queued_inputs=pending,
            )
            if result.run.status != "completed":
                return

    async def _consume_turn_boundary_inputs(
        self,
        emitted: list[EventRecord],
        sequence: int,
        *,
        session: SessionRecord,
        run: RunRecord,
        messages: list[MessageRecord],
        assistant_text: str,
        assistant_reasoning: str,
        assistant_continuation: ProviderContinuationArtifact | None,
        turn_id: str,
    ) -> TurnBoundaryInputResult:
        snapshot = self.store.load_session(session.session_id, recover=False)
        limit = 1 if self.queued_input_config.consumption_mode == "one_by_one" else None
        eligible = self.queued_inputs.eligible(snapshot, mode="steer", limit=limit)
        if not eligible:
            return TurnBoundaryInputResult(sequence=sequence)

        appended: list[MessageRecord] = []
        if assistant_text or assistant_reasoning or assistant_continuation is not None:
            assistant_message = self.run_lifecycle.append_assistant_message(
                session=session,
                run=run,
                text=assistant_text,
                reasoning_content=assistant_reasoning,
                provider_continuation=assistant_continuation,
                sequence=len(messages) + 1,
                turn_id=turn_id,
            )
            if not any(message.message_id == assistant_message.message_id for message in messages):
                appended.append(assistant_message)

        for item in eligible:
            latest = self.store.load_session(session.session_id, recover=False)
            if self._queued_input_is_durable(latest.messages, item.queue_id):
                self.queued_inputs.acknowledge(latest, item.queue_id)
                durable = next(
                    (
                        message
                        for message in latest.messages
                        if message.metadata.get("queued_input_id") == item.queue_id
                        or message.metadata.get("queued_follow_up_id") == item.queue_id
                    ),
                    None,
                )
                if durable is not None and not any(
                    message.message_id == durable.message_id for message in messages
                ):
                    appended.append(durable)
                continue
            user_message = self.run_lifecycle.append_user_message(
                session=session,
                run=run,
                prompt=item.prompt,
                sequence=len(messages) + len(appended) + 1,
                resource_refs=item.resource_refs,
                metadata={
                    "queued_input_id": item.queue_id,
                    "queued_input_mode": item.mode,
                },
            )
            appended.append(user_message)
            latest = self.store.load_session(session.session_id, recover=False)
            self.queued_inputs.acknowledge(latest, item.queue_id)
            sequence = await self.event_recorder.emit(
                emitted,
                sequence,
                session_id=session.session_id,
                run_id=run.run_id,
                event_type="queued_input_consumed",
                payload={
                    "queue_id": item.queue_id,
                    "mode": item.mode,
                    "turn_id": turn_id,
                },
            )
        return TurnBoundaryInputResult(sequence=sequence, messages=appended)

    def _validate_model_resource_refs(
        self,
        session: SessionRecord,
        resource_refs: tuple[str, ...],
        *,
        adapter: RuntimeAdapter | None = None,
    ) -> None:
        if not resource_refs:
            return
        if self.resources is None:
            raise ResourceAttachmentError(
                "resource attachments require a configured ResourceService"
            )
        active_adapter = adapter
        if active_adapter is None:
            selection = self.models.resolve(
                session.current_model or self.models.default_model().key
            )
            active_adapter = self.models.adapter_for(selection.key)
        self.resources.validate_model_references(
            session.session_id,
            resource_refs,
            supports_vision=active_adapter.model.supports_vision,
        )

    @staticmethod
    def _queued_input_is_durable(messages: list[MessageRecord], queue_id: str) -> bool:
        return any(
            message.metadata.get("queued_input_id") == queue_id
            or message.metadata.get("queued_follow_up_id") == queue_id
            for message in messages
        )

    def _acquire_session_run_lock(self, session_id: str, run_id: str) -> SessionLock | None:
        return self.store.acquire_session_lock(session_id, run_id)

    def _release_session_run_lock(self, lock: SessionLock | None) -> None:
        if lock is not None:
            lock.release()

    def _is_session_run_locked(self, session_id: str) -> bool:
        return self.store.is_session_locked(session_id)

    async def approve_tool(
        self,
        *,
        session_id: str,
        run_id: str | None = None,
        approval_id: str | None = None,
        approved: bool,
        reason: str = "User rejected",
        arguments_override: dict[str, object] | None = None,
    ) -> RunResult:
        snapshot = self.store.load_session(session_id)
        session = snapshot.session
        run = find_run(snapshot.runs, run_id) if run_id else find_active_run(snapshot.runs)
        if run.status != "waiting_approval":
            raise ValueError(f"run is not waiting for approval: {run.status}")
        batch = run.active_tool_batch
        if batch is None:
            raise ValueError("run has no active tool batch")
        resolved_approval_id = approval_id or run.pending_approval_id
        if resolved_approval_id is None:
            raise ValueError("run has no pending approval")
        run_lock = self._acquire_session_run_lock(session.session_id, run.run_id)
        if run_lock is None:
            raise SessionBusyError(f"run is already active: {run.run_id}")
        try:
            snapshot = self.store.load_session(session_id, recover=False)
            session = snapshot.session
            run = find_run(snapshot.runs, run.run_id)
            if run.status != "waiting_approval":
                raise ValueError(f"run is not waiting for approval: {run.status}")
            batch = run.active_tool_batch
            if batch is None:
                raise ValueError("run has no active tool batch")
            resolved_approval_id = approval_id or run.pending_approval_id
            if resolved_approval_id is None:
                raise ValueError("run has no pending approval")
            batch = self.tool_calls.resolve_approval(
                batch,
                approval_id=resolved_approval_id,
                approved=approved,
                reason=reason,
                arguments_override=arguments_override,
            )
            resolved_call = next(
                call
                for call in batch.calls
                if call.approval is not None and call.approval.approval_id == resolved_approval_id
            )
            sequence = len(snapshot.events)
            emitted: list[EventRecord] = []
            sequence = await self.event_recorder.emit(
                emitted,
                sequence,
                session_id=session.session_id,
                run_id=run.run_id,
                event_type="approval_resolved",
                payload={
                    "batch_id": batch.batch_id,
                    "turn_id": batch.turn_id,
                    "approval_id": resolved_approval_id,
                    "tool_call_id": resolved_call.request.id,
                    "tool_call_status": resolved_call.status,
                    "approved": approved,
                    "reason": None if approved else reason,
                },
            )
            result = await self._continue_resolved_batch(
                snapshot=snapshot,
                session=session,
                run=run,
                batch=batch,
                emitted=emitted,
                sequence=sequence,
            )
        finally:
            self._release_session_run_lock(run_lock)
        # Queue draining starts a new Run and therefore must occur outside the
        # completed interaction's non-reentrant Session run lock.
        if result.run.status == "completed":
            await self._drain_queued_input_queue(result.session.session_id)
        return result

    async def answer_user(
        self,
        *,
        session_id: str,
        run_id: str,
        answer: str,
    ) -> RunResult:
        snapshot = self.store.load_session(session_id)
        session = snapshot.session
        run = find_run(snapshot.runs, run_id)
        if run.status != "waiting_user":
            raise ValueError(f"run is not waiting for user input: {run.status}")
        batch = run.active_tool_batch
        if batch is None:
            raise ValueError("run has no active tool batch")
        waiting_call = next((call for call in batch.calls if call.status == "waiting_user"), None)
        if waiting_call is None:
            raise ValueError("run has no tool call waiting for user input")
        run_lock = self._acquire_session_run_lock(session.session_id, run.run_id)
        if run_lock is None:
            raise SessionBusyError(f"run is already active: {run.run_id}")
        try:
            snapshot = self.store.load_session(session_id, recover=False)
            session = snapshot.session
            run = find_run(snapshot.runs, run.run_id)
            if run.status != "waiting_user":
                raise ValueError(f"run is not waiting for user input: {run.status}")
            batch = run.active_tool_batch
            if batch is None:
                raise ValueError("run has no active tool batch")
            waiting_call = next(
                (call for call in batch.calls if call.status == "waiting_user"),
                None,
            )
            if waiting_call is None:
                raise ValueError("run has no tool call waiting for user input")
            batch = self.tool_calls.resolve_user_answer(
                batch,
                tool_call_id=waiting_call.request.id,
                answer=answer,
            )
            resolved_call = next(
                call for call in batch.calls if call.request.id == waiting_call.request.id
            )
            sequence = len(snapshot.events)
            emitted: list[EventRecord] = []
            sequence = await self.event_recorder.emit(
                emitted,
                sequence,
                session_id=session.session_id,
                run_id=run.run_id,
                event_type="ask_user_resolved",
                payload={
                    "batch_id": batch.batch_id,
                    "turn_id": batch.turn_id,
                    "tool_call_id": waiting_call.request.id,
                    "tool_call_status": resolved_call.status,
                    "answer": answer,
                },
            )
            result = await self._continue_resolved_batch(
                snapshot=snapshot,
                session=session,
                run=run,
                batch=batch,
                emitted=emitted,
                sequence=sequence,
            )
        finally:
            self._release_session_run_lock(run_lock)
        # Keep the same post-lock Run boundary as normal runs and approval
        # recovery; a follow-up must acquire a fresh Session run lock.
        if result.run.status == "completed":
            await self._drain_queued_input_queue(result.session.session_id)
        return result

    async def _continue_resolved_batch(
        self,
        *,
        snapshot: SessionSnapshot,
        session: SessionRecord,
        run: RunRecord,
        batch: ToolCallBatchRecord,
        emitted: list[EventRecord],
        sequence: int,
    ) -> RunResult:
        pending_approvals = batch.pending_approvals
        waiting_user = next((call for call in batch.calls if call.status == "waiting_user"), None)
        if pending_approvals or waiting_user is not None:
            status: RunStatus = "waiting_approval" if pending_approvals else "waiting_user"
            session, run = self.run_lifecycle.save_batch_state(
                session=session,
                run=run,
                batch=batch,
                status=status,
                phase="human_interaction",
            )
            sequence = await self.event_recorder.emit(
                emitted,
                sequence,
                session_id=session.session_id,
                run_id=run.run_id,
                event_type="run_status_changed",
                payload={
                    "to_status": status,
                    "status": status,
                    "batch_id": batch.batch_id,
                    "turn_id": batch.turn_id,
                },
            )
            return RunResult(session=session, run=run, events=emitted)

        active_signal = self._activate_abort_signal(session.session_id, run.run_id)
        try:
            try:
                handling = await self.tool_calls.execute_batch(
                    emitted,
                    sequence,
                    session=session,
                    run=run,
                    batch=batch,
                    message_sequence=len(snapshot.messages) + 1,
                    abort_signal=active_signal,
                )
            except Exception as exc:  # noqa: BLE001 - coordinator is a runtime boundary.
                latest = self.store.load_session(session.session_id, recover=False)
                latest_run = find_run(latest.runs, run.run_id)
                failed = await self.run_lifecycle.fail(
                    emitted,
                    len(latest.events),
                    session=latest.session,
                    run=latest_run,
                    error=f"tool batch handling failed: {exc}",
                    final_text="",
                    tool_results=[],
                )
                self._finish_run_hooks(failed.run)
                return failed
            latest = self.store.load_session(session.session_id)
            session = latest.session
            run = find_run(latest.runs, run.run_id)
            abort_result = self._aborted_result(
                session_id=session.session_id,
                run_id=run.run_id,
                final_text="",
                emitted=emitted,
                tool_results=handling.results,
            )
            if abort_result is not None:
                self._finish_run_hooks(abort_result.run)
                return abort_result
            boundary = await self._consume_turn_boundary_inputs(
                emitted,
                handling.sequence,
                session=session,
                run=run,
                messages=latest.messages,
                assistant_text="",
                assistant_reasoning="",
                assistant_continuation=None,
                turn_id=batch.turn_id,
            )
            latest = self.store.load_session(session.session_id, recover=False)
            session = latest.session
            run = find_run(latest.runs, run.run_id)
            session, active_adapter = self._resolve_session_adapter(session)
            resumed = await self._continue_model_loop(
                session=session,
                run=run,
                messages=latest.messages,
                sequence=boundary.sequence,
                emitted=emitted,
                tool_results=handling.results,
                abort_signal=active_signal,
                adapter=active_adapter,
            )
        finally:
            self._release_abort_signal(run.run_id, active_signal)
        self._finish_run_hooks(resumed.run)
        return resumed

    def _is_run_aborted(self, session_id: str, run_id: str) -> bool:
        return self._abort_state(session_id, run_id) is not None

    def _abort_state(
        self,
        session_id: str,
        run_id: str,
    ) -> tuple[SessionRecord, RunRecord] | None:
        try:
            run = self.store.load_run(session_id, run_id)
        except Exception:  # noqa: BLE001 - abort polling must not break active runs.
            return None
        if run.status == "aborted" or run.abort_requested:
            try:
                return self.store.load_session_record(session_id), run
            except Exception:  # noqa: BLE001 - abort polling must not break active runs.
                return None
        return None

    def _aborted_result(
        self,
        *,
        session_id: str,
        run_id: str,
        final_text: str,
        emitted: list[EventRecord],
        tool_results: list[ToolResult],
    ) -> RunResult | None:
        abort_state = self._abort_state(session_id, run_id)
        if abort_state is None:
            return None
        session, run = abort_state
        return RunResult(
            session=session,
            run=run,
            final_text=final_text,
            events=emitted,
            tool_results=tool_results,
        )

    def _activate_abort_signal(self, session_id: str, run_id: str) -> AbortSignal:
        signal = AbortSignal(lambda: self._abort_state(session_id, run_id) is not None)
        self._abort_signals[run_id] = signal
        return signal

    def _release_abort_signal(self, run_id: str, signal: AbortSignal) -> None:
        if self._abort_signals.get(run_id) is signal:
            self._abort_signals.pop(run_id, None)

    def _finish_run_hooks(self, run: RunRecord) -> None:
        self.run_lifecycle.finish_run(run)
        if run.status not in ACTIVE_RUN_STATUSES:
            self.hooks.finish_run(run.run_id)


def _title_from_prompt(prompt: str) -> str:
    stripped = " ".join(prompt.split())
    return stripped[:60] if stripped else "New session"
