from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from functools import partial
from typing import Literal, Protocol, cast

from pydantic import JsonValue

from ouro.application.agent_definitions import AgentDefinitionService
from ouro.application.agent_models import (
    AgentHandleView,
    AgentInteractionView,
    AgentReadView,
    AgentTreeSnapshot,
    AgentWaitResultView,
    SpawnAgentRequest,
)
from ouro.application.agent_recovery import AgentRecoveryService
from ouro.application.agent_tree import (
    AgentTreeProjector,
    AgentTreeSubscription,
    RuntimeSubscriptionPort,
    delegation_status,
)
from ouro.application.dto import Diagnostic
from ouro.application.events import ApplicationEventPublisher
from ouro.application.supervisor_models import (
    HostInputReceipt,
    RunHandle,
    RuntimeActivitySnapshot,
    RuntimeStatusSnapshot,
    SupervisorStartResult,
)
from ouro.core.agents import (
    AgentCatalogEntry,
    AgentCatalogSnapshot,
    AgentControlRequest,
    AgentControlResult,
    AgentSpawnPolicy,
    AgentWaitRequest,
    AgentWaitResolution,
    DelegatedConstraints,
    DelegationRecord,
    DelegationResult,
    DeliveryPolicy,
    ResultDelivery,
    delivery_policy_for_execution_mode,
)
from ouro.core.base import new_id, utc_now
from ouro.core.errors import DelegationRevisionConflictError, PersistenceError, SessionBusyError
from ouro.core.run_budget import RunBudgetPolicy, RunBudgetReason
from ouro.core.session_controls import DelegatedTaskInput, HostSessionInput
from ouro.core.state import ACTIVE_RUN_STATUSES, RunRecord, SessionRecord, session_status_for_run
from ouro.core.tool_batches import ToolCallBatchRecord
from ouro.core.tools import ToolInvocation, ToolResult
from ouro.sessions import SessionStore


class RunScheduler(Protocol):
    async def start(self) -> SupervisorStartResult: ...

    async def submit_prepared(
        self,
        run: RunRecord,
        session_input: HostSessionInput | DelegatedTaskInput,
    ) -> RunHandle: ...

    async def wait(self, handle: RunHandle) -> RuntimeStatusSnapshot: ...

    async def abort(
        self, handle_or_session: RunHandle | str, *, reason: str = "User aborted"
    ) -> RuntimeStatusSnapshot: ...

    def activity(self, handle_or_session: RunHandle | str) -> RuntimeActivitySnapshot: ...

    def subscribe(
        self,
        session_id: str,
        *,
        after_sequence: int = 0,
        max_buffer: int = 256,
        include_activity: bool = False,
    ) -> RuntimeSubscriptionPort: ...

    async def resume_agent_wait(
        self,
        *,
        session_id: str,
        run_id: str,
        tool_call_id: str,
        resolution: AgentWaitResolution,
        result: ToolResult,
    ) -> RunHandle: ...

    async def route_host_input(
        self,
        session_input: HostSessionInput,
        *,
        session_id: str,
    ) -> HostInputReceipt: ...

    async def resolve_tool_approval(
        self,
        *,
        session_id: str,
        run_id: str,
        approval_id: str,
        approved: bool,
        reason: str = "User rejected",
        arguments_override: dict[str, object] | None = None,
    ) -> RunHandle: ...

    async def resolve_tool_input(
        self,
        *,
        session_id: str,
        run_id: str,
        answer: JsonValue,
    ) -> RunHandle: ...


class AgentResourcePort(Protocol):
    def validate_references(
        self,
        session_id: str,
        resource_ids: Iterable[str],
    ) -> None: ...

    def copy_references(
        self,
        source_session_id: str,
        target_session_id: str,
        resource_ids: Iterable[str],
    ) -> tuple[str, ...]: ...


@dataclass(frozen=True)
class _RootBudgetState:
    policy: RunBudgetPolicy
    input_tokens: int
    output_tokens: int
    model_rounds: int
    elapsed_seconds: float


class AgentService:
    """Orchestrate durable Delegations over ordinary Session and Run records."""

    def __init__(
        self,
        *,
        store: SessionStore,
        definitions: AgentDefinitionService,
        scheduler: RunScheduler,
        events: ApplicationEventPublisher,
        resources: AgentResourcePort | None = None,
        spawn_policy: AgentSpawnPolicy | None = None,
        max_depth: int = 1,
        cascade_timeout_seconds: float = 5.0,
    ) -> None:
        if max_depth != 1:
            raise ValueError("first-release AgentService supports only max_depth=1")
        if cascade_timeout_seconds <= 0:
            raise ValueError("cascade_timeout_seconds must be positive")
        self.store = store
        self.definitions = definitions
        self.scheduler = scheduler
        self.events = events
        self.resources = resources
        self.spawn_policy = spawn_policy or AgentSpawnPolicy()
        self.max_depth = max_depth
        self.cascade_timeout_seconds = cascade_timeout_seconds
        self.recovery = AgentRecoveryService(store)
        self._run_handles: dict[str, RunHandle] = {}
        self._completion_events: dict[str, asyncio.Event] = {}
        self._watchers: set[asyncio.Task[None]] = set()
        self._armed_wait_ids: set[str] = set()
        self._tree_projector = AgentTreeProjector(store=store, runtime=scheduler)
        self._tree_subscriptions: set[AgentTreeSubscription] = set()
        self._spawn_locks: dict[str, asyncio.Lock] = {}
        self._result_locks: dict[str, asyncio.Lock] = {}
        self._closed = False

    def constrain_catalog(self, catalog: AgentCatalogSnapshot) -> AgentCatalogSnapshot:
        """Freeze the exact definitions and tool capabilities this service can spawn."""

        entries = tuple(
            entry.model_copy(
                update={
                    "effective_tools": tuple(
                        tool
                        for tool in entry.effective_tools
                        if self.spawn_policy.allows_tool(tool)
                    )
                }
            )
            for entry in catalog.entries
            if self.spawn_policy.allows_agent(entry.definition.agent_id)
            and self.spawn_policy.allows_model(entry.definition.model)
        )
        return AgentCatalogSnapshot(
            revision=catalog.revision,
            entries=entries,
            spawn_policy=self.spawn_policy,
            created_at=catalog.created_at,
        )

    async def execute_control(
        self,
        request: AgentControlRequest,
        *,
        invocation: ToolInvocation,
        session: SessionRecord,
        run: RunRecord,
    ) -> AgentControlResult:
        """Execute a validated product control intent for one parent ToolCall."""

        if invocation.session_id != session.session_id or invocation.run_id != run.run_id:
            raise ValueError("Agent control invocation does not match its parent Run")
        if request.action == "spawn":
            assert request.agent_id is not None
            assert request.task is not None
            assert request.description is not None
            handle = await self.spawn_agent(
                SpawnAgentRequest(
                    request_id=request.request_id,
                    parent_session_id=session.session_id,
                    parent_run_id=run.run_id,
                    parent_tool_call_id=invocation.tool_call_id,
                    agent_id=request.agent_id,
                    task=request.task,
                    description=request.description,
                    nickname=request.nickname,
                    execution_mode=request.execution_mode,
                    resource_refs=request.resource_refs,
                )
            )
            if request.execution_mode == "sync":
                return AgentControlResult(
                    status="waiting_child",
                    wait=AgentWaitRequest(
                        delegation_id=handle.delegation_id,
                    ),
                )
            structured = {"handle": handle.model_dump(mode="json")}
            return AgentControlResult(
                content=_async_spawn_content(handle),
                structured=structured,
            )
        if request.action == "send":
            assert request.delegation_id is not None
            assert request.message is not None
            handle = await self.send_agent_message(
                request.delegation_id,
                request.message,
                request_id=request.request_id,
                mode=request.input_mode,
                parent_session_id=session.session_id,
            )
            return AgentControlResult(
                content=_send_agent_message_content(handle),
                structured={"handle": handle.model_dump(mode="json")},
            )
        assert request.action == "interrupt"
        assert request.delegation_id is not None
        self._require_parent(request.delegation_id, session.session_id)
        handle = await self.interrupt_agent(
            request.delegation_id,
            reason=request.reason or "Agent interrupted by parent",
        )
        return AgentControlResult(
            content=json.dumps(handle.model_dump(mode="json"), ensure_ascii=False),
            structured={"handle": handle.model_dump(mode="json")},
        )

    async def wait_parked(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        batch: ToolCallBatchRecord,
        tool_call_id: str,
        wait: AgentWaitRequest,
    ) -> None:
        """Arm a durable wait only after the parent batch has been persisted."""

        durable = self.store.load_run(session.session_id, run.run_id)
        call = next(
            (
                item
                for item in (durable.active_tool_batch or batch).calls
                if item.request.id == tool_call_id
            ),
            None,
        )
        if (
            durable.status != "waiting_child"
            or call is None
            or call.status != "waiting_child"
            or call.agent_wait != wait
        ):
            raise ValueError("parent Agent wait was not durably parked")
        if call.request.name != "SpawnAgent":
            raise ValueError("durable Agent wait must belong to one synchronous SpawnAgent call")
        if wait.wait_id in self._armed_wait_ids:
            return
        self._armed_wait_ids.add(wait.wait_id)
        waiter = asyncio.create_task(
            self._await_agent_wait(
                session_id=session.session_id,
                run_id=run.run_id,
                tool_call_id=tool_call_id,
                tool_name=call.request.name,
                wait=wait,
            ),
            name=f"ouro-agent-wait-{wait.wait_id}",
        )
        self._watchers.add(waiter)
        waiter.add_done_callback(partial(self._wait_finished, wait_id=wait.wait_id))

    async def start(self) -> tuple[Diagnostic, ...]:
        started = await self.scheduler.start()
        return (*started.diagnostics, *(await self.reconcile()))

    async def close(self) -> None:
        self._closed = True
        self._spawn_locks.clear()
        for subscription in tuple(self._tree_subscriptions):
            subscription.close()
        self._tree_subscriptions.clear()
        if self._watchers:
            for watcher in tuple(self._watchers):
                watcher.cancel()
            await asyncio.gather(*self._watchers, return_exceptions=True)
            self._watchers.clear()
        self._result_locks.clear()

    async def settle_terminal_run(self, run: RunRecord) -> None:
        """Settle subagent results and bound children at a durable Run boundary."""

        if run.status in ACTIVE_RUN_STATUSES:
            return
        parent_session = self.store.load_session_record(run.session_id)
        if parent_session.session_kind == "subagent":
            assert parent_session.root_session_id is not None
            relation = self.store.find_delegation_by_child_session(
                parent_session.root_session_id,
                parent_session.session_id,
            )
            if relation is not None and relation.current_child_run_id == run.run_id:
                await self._finalize_result(relation.delegation_id)
                self._completion_event(relation.delegation_id).set()
            return
        if run.status not in {"aborted", "failed"}:
            return
        root_id = parent_session.root_session_id or parent_session.session_id
        self._resolve_interrupted_parent_wait(run.session_id, run.run_id)
        relations = [
            relation
            for relation in self.store.list_delegations(
                root_id,
                parent_session_id=run.session_id,
            )
            if relation.parent_run_id == run.run_id
            and relation.lifecycle_binding == "bound"
            and not self._is_terminal(relation)
        ]
        results = await asyncio.gather(
            *(
                self._cascade_bound_child(relation, parent_run_id=run.run_id)
                for relation in relations
            ),
            return_exceptions=True,
        )
        for relation, result in zip(relations, results, strict=True):
            if not isinstance(result, BaseException):
                continue
            timed_out = isinstance(result, TimeoutError)
            await self.events.publish(
                session_id=relation.root_session_id,
                run_id=run.run_id,
                event_type="diagnostic",
                payload={
                    "code": "cascade_timeout" if timed_out else "bound_cascade_failed",
                    "severity": "warning",
                    "component": "application.agents",
                    "delegation_id": relation.delegation_id,
                    "child_session_id": relation.child_session_id,
                    "parent_run_id": run.run_id,
                    "error_type": type(result).__name__,
                    "recoverable": True,
                    "suggested_action": ("Inspect the child Run and retry interrupt explicitly."),
                },
            )

    async def _cascade_bound_child(
        self,
        relation: DelegationRecord,
        *,
        parent_run_id: str,
    ) -> None:
        if relation.child_session_id is None:
            raise ValueError("bound Delegation has no child Session")
        async with asyncio.timeout(self.cascade_timeout_seconds):
            durable = self._load_by_id(relation.delegation_id)
            if durable.child_session_id is None:
                raise ValueError("bound Delegation has no child Session")
            await self.scheduler.abort(
                self._run_handles.get(relation.delegation_id) or durable.child_session_id,
                reason=f"Bound parent Run {parent_run_id} terminated",
            )
            await self._finalize_result(relation.delegation_id)

    async def spawn_agent(self, request: SpawnAgentRequest) -> AgentHandleView:
        if self._closed:
            raise RuntimeError("AgentService is closed")
        parent = self.store.load_session_record(request.parent_session_id)
        root_id = parent.root_session_id or parent.session_id
        lock = self._spawn_locks.setdefault(root_id, asyncio.Lock())
        async with lock:
            return await self._spawn_agent_serialized(request)

    async def _spawn_agent_serialized(
        self,
        request: SpawnAgentRequest,
    ) -> AgentHandleView:
        if self._closed:
            raise RuntimeError("AgentService is closed")
        await self.scheduler.start()
        parent_session = self.store.load_session_record(request.parent_session_id)
        parent_run = self.store.load_run(request.parent_session_id, request.parent_run_id)
        active_parent = (
            parent_run.status in ACTIVE_RUN_STATUSES
            and parent_session.active_run_id == parent_run.run_id
        )
        host_completed_parent = (
            request.parent_tool_call_id is None and parent_run.status == "completed"
        )
        if not active_parent and not host_completed_parent:
            raise SessionBusyError(
                "parent run must be active for model delegation or completed for Host "
                f"delegation: {parent_run.run_id} ({parent_run.status})"
            )
        if parent_session.session_kind == "subagent":
            raise ValueError(f"maximum subagent depth is {self.max_depth}")
        if request.lifecycle_binding != "bound":
            raise ValueError("detached subagent is not supported in the first release")
        entry = self._resolve_parent_catalog(parent_run, request.agent_id)
        assert parent_run.agent_catalog is not None
        policy = parent_run.agent_catalog.spawn_policy
        if request.execution_mode not in policy.allowed_execution_modes:
            raise ValueError(f"subagent execution mode is not allowed: {request.execution_mode}")
        if request.lifecycle_binding not in policy.allowed_lifecycle_bindings:
            raise ValueError(
                f"subagent lifecycle binding is not allowed: {request.lifecycle_binding}"
            )
        root_id = parent_session.root_session_id or parent_session.session_id
        budget = self._effective_child_budget(
            root_session_id=root_id,
            parent_run=parent_run,
            requested=entry.definition.default_budget,
        )
        if request.resource_refs:
            if self.resources is None:
                raise ValueError("Agent resource delegation requires ResourceService")
            self.resources.validate_references(
                parent_session.session_id,
                request.resource_refs,
            )
        existing = self.store.find_delegation_by_idempotency_key(
            root_id,
            request.request_id,
        )
        if existing is not None:
            self._validate_spawn_retry(
                existing,
                request=request,
                parent_session=parent_session,
                entry=entry,
                allow_external_paths=policy.allow_external_paths,
            )
            return await self._commit_spawn(
                existing,
                request=request,
                parent_session=parent_session,
                parent_run=parent_run,
            )
        delegation = DelegationRecord(
            idempotency_key=request.request_id,
            root_session_id=root_id,
            parent_session_id=parent_session.session_id,
            parent_run_id=parent_run.run_id,
            parent_tool_call_id=request.parent_tool_call_id,
            child_session_id=new_id("ses"),
            current_child_run_id=new_id("run"),
            agent_id=entry.definition.agent_id,
            nickname=request.nickname,
            task_description=request.description,
            prompt_digest=("sha256:" + hashlib.sha256(request.task.encode("utf-8")).hexdigest()),
            task_resource_refs=request.resource_refs,
            execution_mode=request.execution_mode,
            lifecycle_binding=request.lifecycle_binding,
            delivery_policy=(
                "notify_only"
                if request.parent_tool_call_id is None
                else delivery_policy_for_execution_mode(request.execution_mode)
            ),
            depth=1,
            definition=entry.definition,
            effective_tools=entry.effective_tools,
            constraints=DelegatedConstraints(
                workspace=parent_session.workspace,
                allow_external_paths=policy.allow_external_paths,
            ),
            approval_mode=parent_session.approval_mode,
            interaction_mode=(
                "unattended" if request.unattended else parent_session.interaction_mode
            ),
            budget=budget,
        )
        delegation = self.store.create_delegation(delegation)
        return await self._commit_spawn(
            delegation,
            request=request,
            parent_session=parent_session,
            parent_run=parent_run,
        )

    def _validate_spawn_retry(
        self,
        relation: DelegationRecord,
        *,
        request: SpawnAgentRequest,
        parent_session: SessionRecord,
        entry: AgentCatalogEntry,
        allow_external_paths: bool,
    ) -> None:
        root_id = parent_session.root_session_id or parent_session.session_id
        prompt_digest = "sha256:" + hashlib.sha256(request.task.encode("utf-8")).hexdigest()
        matches = (
            relation.root_session_id == root_id
            and relation.parent_session_id == request.parent_session_id
            and relation.parent_run_id == request.parent_run_id
            and relation.parent_tool_call_id == request.parent_tool_call_id
            and relation.agent_id == request.agent_id
            and relation.nickname == request.nickname
            and relation.task_description == request.description
            and relation.prompt_digest == prompt_digest
            and relation.task_resource_refs == request.resource_refs
            and relation.execution_mode == request.execution_mode
            and relation.lifecycle_binding == request.lifecycle_binding
            and relation.definition.contract_hash == entry.definition.contract_hash
            and relation.effective_tools == entry.effective_tools
            and relation.constraints.allow_external_paths == allow_external_paths
        )
        if not matches:
            raise ValueError(
                "SpawnAgent request_id is already bound to a different durable request"
            )

    async def _commit_spawn(
        self,
        relation: DelegationRecord,
        *,
        request: SpawnAgentRequest,
        parent_session: SessionRecord,
        parent_run: RunRecord,
    ) -> AgentHandleView:
        """Resume the durable Spawn protocol from its last committed phase."""

        if relation.child_session_id is None or relation.current_child_run_id is None:
            relation = self.store.save_delegation(
                relation.model_copy(
                    update={
                        "child_session_id": relation.child_session_id or new_id("ses"),
                        "current_child_run_id": relation.current_child_run_id or new_id("run"),
                        "spawn_state": "requested",
                        "error": None,
                    }
                ),
                expected_revision=relation.revision,
            )

        child = self._ensure_spawn_child(
            relation,
            parent_session=parent_session,
        )
        if relation.spawn_state not in {"child_created", "submitted"}:
            relation = self.store.save_delegation(
                relation.model_copy(update={"spawn_state": "child_created", "error": None}),
                expected_revision=relation.revision,
            )
        if request.resource_refs:
            assert self.resources is not None
            self.resources.copy_references(
                parent_session.session_id,
                child.session_id,
                request.resource_refs,
            )

        run = self._ensure_spawn_run(
            relation,
            child=child,
            parent_run=parent_run,
        )
        if relation.spawn_state == "submitted":
            if run.status not in ACTIVE_RUN_STATUSES and relation.result is None:
                await self._finalize_result(relation.delegation_id)
            return self._handle(self._load_by_id(relation.delegation_id))
        if run.status != "queued":
            latest = self._load_by_id(relation.delegation_id)
            relation = self.store.save_delegation(
                latest.model_copy(update={"spawn_state": "submitted", "error": None}),
                expected_revision=latest.revision,
            )
            if run.status not in ACTIVE_RUN_STATUSES and relation.result is None:
                relation = await self._finalize_result(relation.delegation_id)
            return self._handle(relation)

        delegated_input = DelegatedTaskInput(
            prompt=request.task,
            mode="follow_up",
            title=request.nickname or relation.definition.name,
            delegation_id=relation.delegation_id,
            resource_refs=request.resource_refs,
        )
        try:
            handle = await self.scheduler.submit_prepared(run, delegated_input)
        except Exception as exc:
            latest = self._load_by_id(relation.delegation_id)
            self.store.save_delegation(
                latest.model_copy(
                    update={
                        "spawn_state": "failed",
                        "error": f"child submission failed: {type(exc).__name__}",
                    }
                ),
                expected_revision=latest.revision,
            )
            raise

        latest = self._load_by_id(relation.delegation_id)
        relation = self.store.save_delegation(
            latest.model_copy(update={"spawn_state": "submitted", "error": None}),
            expected_revision=latest.revision,
        )
        self._run_handles[relation.delegation_id] = handle
        self._completion_event(relation.delegation_id).clear()
        await self.events.publish(
            session_id=relation.root_session_id,
            run_id=parent_run.run_id,
            event_type="agent_spawned",
            payload={
                "delegation_id": relation.delegation_id,
                "child_session_id": child.session_id,
                "child_run_id": run.run_id,
                "agent_id": relation.agent_id,
                "execution_mode": relation.execution_mode,
            },
        )
        self._watch(relation.delegation_id, handle)
        return self._handle(relation)

    def _ensure_spawn_child(
        self,
        relation: DelegationRecord,
        *,
        parent_session: SessionRecord,
    ) -> SessionRecord:
        assert relation.child_session_id is not None
        try:
            child = self.store.load_session_record(relation.child_session_id)
        except FileNotFoundError:
            child = SessionRecord(
                session_id=relation.child_session_id,
                session_kind="subagent",
                root_session_id=relation.root_session_id,
                parent_session_id=relation.parent_session_id,
                delegation_id=relation.delegation_id,
                title=relation.nickname or relation.definition.name,
                workspace=relation.constraints.workspace,
                workspace_trusted=parent_session.workspace_trusted,
                current_model=relation.definition.model or parent_session.current_model,
                approval_mode=relation.approval_mode,
                interaction_mode=relation.interaction_mode,
            )
            self.store.create_session(child)
            return child
        if (
            child.session_kind != "subagent"
            or child.root_session_id != relation.root_session_id
            or child.parent_session_id != relation.parent_session_id
            or child.delegation_id != relation.delegation_id
            or child.workspace != relation.constraints.workspace
            or child.approval_mode != relation.approval_mode
            or child.interaction_mode != relation.interaction_mode
        ):
            raise PersistenceError(
                f"reserved child Session does not match Delegation {relation.delegation_id}"
            )
        return child

    def _ensure_spawn_run(
        self,
        relation: DelegationRecord,
        *,
        child: SessionRecord,
        parent_run: RunRecord,
    ) -> RunRecord:
        assert relation.current_child_run_id is not None
        try:
            run = self.store.load_run(child.session_id, relation.current_child_run_id)
        except FileNotFoundError:
            run = RunRecord(
                run_id=relation.current_child_run_id,
                session_id=child.session_id,
                status="queued",
                origin="delegation",
                parent_run_id=parent_run.run_id,
                initiator_id=f"ouro.subagents/{relation.delegation_id}",
                trigger="subagent",
                model=(relation.definition.model or parent_run.model),
                metadata={
                    **self._extension_metadata(parent_run),
                    "delegation_id": relation.delegation_id,
                    "root_session_id": relation.root_session_id,
                    "agent_id": relation.agent_id,
                    "agent_definition_contract_hash": relation.definition.contract_hash,
                    "effective_tool_names": [tool.public_name for tool in relation.effective_tools],
                    "effective_tool_refs": [
                        tool.model_dump(mode="json") for tool in relation.effective_tools
                    ],
                    "delegated_constraints": relation.constraints.model_dump(mode="json"),
                    "agent_system_prompt": relation.definition.system_prompt,
                    "run_budget": relation.budget.model_dump(mode="json"),
                },
            )
            with self.store.session_mutation(child.session_id):
                self.store.save_run(run)
                latest_child = self.store.load_session_record(child.session_id)
                self.store.save_session(
                    latest_child.model_copy(
                        update={
                            "status": session_status_for_run(run.status),
                            "active_run_id": run.run_id,
                            "last_run_id": run.run_id,
                            "updated_at": utc_now(),
                            "last_activity_at": utc_now(),
                        }
                    )
                )
        if not self.recovery.relation_is_consistent(relation):
            raise PersistenceError(
                f"reserved child Run does not match Delegation {relation.delegation_id}"
            )
        if run.status in ACTIVE_RUN_STATUSES:
            with self.store.session_mutation(child.session_id):
                latest_child = self.store.load_session_record(child.session_id)
                if latest_child.active_run_id not in {None, run.run_id}:
                    raise SessionBusyError(
                        f"child Session already has an active Run: {latest_child.active_run_id}"
                    )
                if latest_child.active_run_id != run.run_id:
                    self.store.save_session(
                        latest_child.model_copy(
                            update={
                                "status": session_status_for_run(run.status),
                                "active_run_id": run.run_id,
                                "last_run_id": run.run_id,
                                "updated_at": utc_now(),
                                "last_activity_at": utc_now(),
                            }
                        )
                    )
        return run

    async def wait_agents(
        self,
        delegation_ids: Iterable[str],
    ) -> AgentWaitResultView:
        ids = tuple(dict.fromkeys(delegation_ids))
        if not ids:
            raise ValueError("at least one delegation id is required")
        for delegation_id in ids:
            relation = self._load_by_id(delegation_id)
            if self._is_terminal(relation):
                await self._finalize_result(delegation_id)
            else:
                await self._completion_event(delegation_id).wait()
        handles = tuple(self._handle(self._load_by_id(item)) for item in ids)
        return AgentWaitResultView(
            handles=handles,
            completed=all(self._terminal_status(item.status) for item in handles),
        )

    def list_agents(
        self,
        root_session_id: str,
        *,
        parent_session_id: str | None = None,
    ) -> tuple[AgentHandleView, ...]:
        return tuple(
            self._handle(item)
            for item in self.store.list_delegations(
                root_session_id, parent_session_id=parent_session_id
            )
        )

    def read_agent(self, delegation_id: str) -> AgentReadView:
        relation = self._load_by_id(delegation_id)
        final_text = ""
        result_message_id = None
        terminal_status = None
        if relation.result is not None:
            result_message_id = relation.result.result_message_id
            terminal_status = relation.result.terminal_status
            if result_message_id is not None and relation.child_session_id is not None:
                snapshot = self.store.load_runtime_session(relation.child_session_id)
                result_message = next(
                    (
                        message
                        for message in snapshot.messages
                        if message.message_id == result_message_id
                    ),
                    None,
                )
                if result_message is not None:
                    final_text = result_message.text_content
        return AgentReadView(
            handle=self._handle(relation),
            final_text=final_text,
            result_message_id=result_message_id,
            terminal_status=terminal_status,
        )

    async def interrupt_agent(
        self,
        delegation_id: str,
        *,
        reason: str = "Agent interrupted",
    ) -> AgentHandleView:
        lock = self._result_locks.setdefault(delegation_id, asyncio.Lock())
        async with lock:
            relation = self._load_by_id(delegation_id)
            if relation.child_session_id is None:
                raise ValueError("delegation has no child Session")
            handle = self._run_handles.get(delegation_id)
            child_session_id = relation.child_session_id
        await self.scheduler.abort(handle or child_session_id, reason=reason)
        await self._finalize_result(delegation_id)
        return self._handle(self._load_by_id(delegation_id))

    async def send_agent_message(
        self,
        delegation_id: str,
        message: str,
        *,
        request_id: str,
        mode: Literal["steer", "follow_up"] = "steer",
        parent_session_id: str | None = None,
    ) -> AgentHandleView:
        lock = self._result_locks.setdefault(delegation_id, asyncio.Lock())
        async with lock:
            return await self._send_agent_message_serialized(
                delegation_id,
                message,
                request_id=request_id,
                mode=mode,
                parent_session_id=parent_session_id,
            )

    async def _send_agent_message_serialized(
        self,
        delegation_id: str,
        message: str,
        *,
        request_id: str,
        mode: Literal["steer", "follow_up"],
        parent_session_id: str | None,
    ) -> AgentHandleView:
        if not message.strip():
            raise ValueError("Agent message cannot be blank")
        relation = self._load_by_id(delegation_id)
        if parent_session_id is not None and relation.parent_session_id != parent_session_id:
            raise ValueError("delegation is not a direct child of this parent")
        if relation.child_session_id is None:
            raise ValueError("delegation has no child Session")
        status = self._status(relation)
        if status in ACTIVE_RUN_STATUSES:
            if mode != "steer":
                raise SessionBusyError("active child messages must use steer mode")
            await self.scheduler.route_host_input(
                HostSessionInput(
                    prompt=message,
                    mode="steer",
                    host_source_id=(f"ouro.subagents/{delegation_id}/messages/{request_id}"),
                    semantic_tag="subagent-message",
                ),
                session_id=relation.child_session_id,
            )
            return self._handle(relation)
        if mode != "follow_up":
            raise SessionBusyError("idle child messages must use follow_up mode")
        if relation.current_child_run_id is None:
            raise ValueError("delegation has no child Run to resume")

        child = self.store.load_session_record(relation.child_session_id)
        previous_run = self.store.load_run(
            relation.child_session_id,
            relation.current_child_run_id,
        )
        run = RunRecord(
            session_id=child.session_id,
            status="queued",
            origin="host",
            parent_run_id=relation.parent_run_id,
            initiator_id=f"ouro.subagents/{delegation_id}",
            trigger="subagent_follow_up",
            model=relation.definition.model or child.current_model,
            metadata={
                **self._extension_metadata(previous_run),
                "delegation_id": relation.delegation_id,
                "root_session_id": relation.root_session_id,
                "agent_id": relation.agent_id,
                "agent_definition_contract_hash": relation.definition.contract_hash,
                "effective_tool_names": [tool.public_name for tool in relation.effective_tools],
                "effective_tool_refs": [
                    tool.model_dump(mode="json") for tool in relation.effective_tools
                ],
                "delegated_constraints": relation.constraints.model_dump(mode="json"),
                "agent_system_prompt": relation.definition.system_prompt,
                "run_budget": relation.budget.model_dump(mode="json"),
            },
        )
        with self.store.session_mutation(child.session_id):
            self.store.save_run(run)
            latest_child = self.store.load_session_record(child.session_id)
            self.store.save_session(
                latest_child.model_copy(
                    update={
                        "status": "running",
                        "active_run_id": run.run_id,
                        "last_run_id": run.run_id,
                        "updated_at": utc_now(),
                        "last_activity_at": utc_now(),
                    }
                )
            )
        relation = self.store.save_delegation(
            relation.model_copy(
                update={
                    "current_child_run_id": run.run_id,
                    "result": None,
                    "control_state": "open",
                }
            ),
            expected_revision=relation.revision,
        )
        handle = await self.scheduler.submit_prepared(
            run,
            HostSessionInput(
                prompt=message,
                mode="follow_up",
                host_source_id=f"ouro.subagents/{delegation_id}/messages/{request_id}",
                semantic_tag="subagent-message",
            ),
        )
        self._run_handles[delegation_id] = handle
        self._completion_event(delegation_id).clear()
        self._watch(delegation_id, handle)
        return self._handle(relation)

    async def close_agent(self, delegation_id: str) -> AgentHandleView:
        lock = self._result_locks.setdefault(delegation_id, asyncio.Lock())
        async with lock:
            return await self._close_agent_serialized(delegation_id)

    async def _close_agent_serialized(self, delegation_id: str) -> AgentHandleView:
        relation = self._load_by_id(delegation_id)
        if not self._is_terminal(relation):
            raise SessionBusyError("active child must be interrupted before close")
        if relation.control_state == "closed":
            return self._handle(relation)
        updated = self.store.save_delegation(
            relation.model_copy(update={"control_state": "closed"}),
            expected_revision=relation.revision,
        )
        await self.events.publish(
            session_id=updated.root_session_id,
            run_id=updated.parent_run_id,
            event_type="agent_closed",
            payload={"delegation_id": updated.delegation_id},
        )
        return self._handle(updated)

    def agent_tree_snapshot(self, root_session_id: str) -> AgentTreeSnapshot:
        return self._tree_projector.snapshot(root_session_id)

    def subscribe_agent_tree(
        self,
        root_session_id: str,
        *,
        max_buffer: int = 64,
        runtime_buffer: int = 64,
    ) -> AgentTreeSubscription:
        if self._closed:
            raise RuntimeError("AgentService is closed")
        subscription = AgentTreeSubscription(
            root_session_id=root_session_id,
            projector=self._tree_projector,
            runtime=self.scheduler,
            max_buffer=max_buffer,
            runtime_buffer=runtime_buffer,
            on_close=self._tree_subscriptions.discard,
        )
        self._tree_subscriptions.add(subscription)
        return subscription

    def list_agent_interactions(
        self,
        root_session_id: str,
    ) -> tuple[AgentInteractionView, ...]:
        return self._tree_projector.interactions(root_session_id)

    async def resolve_agent_approval(
        self,
        delegation_id: str,
        *,
        child_session_id: str,
        child_run_id: str,
        tool_call_id: str,
        approval_id: str,
        approved: bool,
        reason: str = "User rejected",
        arguments_override: dict[str, object] | None = None,
    ) -> RunHandle:
        relation = self._require_child_identity(
            delegation_id,
            child_session_id=child_session_id,
            child_run_id=child_run_id,
        )
        run = self.store.load_run(child_session_id, child_run_id)
        call = next(
            (
                item
                for item in (run.active_tool_batch.calls if run.active_tool_batch else ())
                if item.request.id == tool_call_id
                and item.approval is not None
                and item.approval.approval_id == approval_id
            ),
            None,
        )
        if call is None or run.status != "waiting_approval":
            raise ValueError("approval identity does not match the child canonical request")
        handle = await self.scheduler.resolve_tool_approval(
            session_id=child_session_id,
            run_id=child_run_id,
            approval_id=approval_id,
            approved=approved,
            reason=reason,
            arguments_override=arguments_override,
        )
        self._run_handles[relation.delegation_id] = handle
        self._completion_event(relation.delegation_id).clear()
        self._watch(relation.delegation_id, handle)
        return handle

    async def resolve_agent_input(
        self,
        delegation_id: str,
        *,
        child_session_id: str,
        child_run_id: str,
        tool_call_id: str,
        request_id: str,
        answer: JsonValue,
    ) -> RunHandle:
        relation = self._require_child_identity(
            delegation_id,
            child_session_id=child_session_id,
            child_run_id=child_run_id,
        )
        run = self.store.load_run(child_session_id, child_run_id)
        batch = run.active_tool_batch
        call = next(
            (
                item
                for item in (batch.calls if batch else ())
                if item.request.id == tool_call_id
                and item.status == "waiting_user"
                and item.input_request is not None
            ),
            None,
        )
        expected_request_id = f"{batch.batch_id}/{tool_call_id}" if batch else None
        if call is None or run.status != "waiting_user" or request_id != expected_request_id:
            raise ValueError("input identity does not match the child canonical request")
        handle = await self.scheduler.resolve_tool_input(
            session_id=child_session_id,
            run_id=child_run_id,
            answer=answer,
        )
        self._run_handles[relation.delegation_id] = handle
        self._completion_event(relation.delegation_id).clear()
        self._watch(relation.delegation_id, handle)
        return handle

    async def reconcile(self) -> tuple[Diagnostic, ...]:
        diagnostics = list(self.recovery.reconcile())
        relations = self._all_relations()
        terminal_parents: dict[tuple[str, str], RunRecord] = {}
        for relation in relations:
            try:
                parent_run = self.store.load_run(
                    relation.parent_session_id,
                    relation.parent_run_id,
                )
            except (FileNotFoundError, PersistenceError):
                continue
            if parent_run.status in {"aborted", "failed"}:
                terminal_parents[(parent_run.session_id, parent_run.run_id)] = parent_run
        for parent_run in terminal_parents.values():
            await self.settle_terminal_run(parent_run)

        relations = self._all_relations()
        for relation in relations:
            if not self.recovery.relation_is_consistent(relation):
                continue
            assert relation.child_session_id is not None
            assert relation.current_child_run_id is not None
            child_run = self.store.load_run(
                relation.child_session_id,
                relation.current_child_run_id,
            )
            if (
                child_run.status == "recoverable"
                and child_run.metadata.get("recovered_from_status") is not None
            ):
                diagnostics.append(self._child_recovered_diagnostic(relation, child_run))
            if relation.result is None and child_run.status not in ACTIVE_RUN_STATUSES:
                diagnostics.append(self._missing_result_diagnostic(relation, child_run))
                await self._finalize_result(relation.delegation_id)
        relations = self._all_relations()
        pending_deliveries = [
            relation
            for relation in relations
            if relation.result is not None and relation.result.delivery.status == "pending"
        ]
        for relation in pending_deliveries:
            if self.recovery.relation_is_consistent(relation):
                await self._deliver_result(relation)
        for session in self.store.list_sessions():
            if session.session_kind != "primary" or session.active_run_id is None:
                continue
            try:
                run = self.store.load_run(session.session_id, session.active_run_id)
            except PersistenceError:
                continue
            batch = run.active_tool_batch
            if run.status != "waiting_child" or batch is None:
                continue
            for call in batch.calls:
                if call.status == "waiting_child" and call.agent_wait is not None:
                    wait_error = self._parent_wait_error(session, call.agent_wait)
                    if wait_error is not None:
                        diagnostics.append(
                            self._parent_wait_diagnostic(
                                session=session,
                                run=run,
                                wait_id=call.agent_wait.wait_id,
                                message=wait_error,
                            )
                        )
                        continue
                    await self.wait_parked(
                        session=session,
                        run=run,
                        batch=batch,
                        tool_call_id=call.request.id,
                        wait=call.agent_wait,
                    )
        return tuple(diagnostics)

    def _parent_wait_error(
        self,
        session: SessionRecord,
        wait: AgentWaitRequest,
    ) -> str | None:
        if wait.state != "pending":
            return "Parent ToolCall is waiting on an already resolved Agent wait."
        delegation_id = wait.delegation_id
        try:
            relation = self._load_by_id(delegation_id)
        except FileNotFoundError:
            return f"Agent wait references missing Delegation {delegation_id}."
        if relation.parent_session_id != session.session_id:
            return f"Agent wait references non-child Delegation {delegation_id}."
        if not self.recovery.relation_is_consistent(relation):
            return f"Agent wait references inconsistent Delegation {delegation_id}."
        return None

    @staticmethod
    def _child_recovered_diagnostic(
        relation: DelegationRecord,
        run: RunRecord,
    ) -> Diagnostic:
        previous_status = str(run.metadata["recovered_from_status"])
        return Diagnostic(
            code="child_run_recovered",
            severity="warning",
            component="application.agent_recovery",
            message=(
                f"Child Run was recovered from {previous_status} without replaying execution."
            ),
            session_id=run.session_id,
            run_id=run.run_id,
            resource_id=relation.delegation_id,
            recoverable=True,
            suggested_action="Inspect the child Run before an explicit retry or resume.",
            metadata={
                "root_session_id": relation.root_session_id,
                "recovered_from_status": previous_status,
            },
        )

    @staticmethod
    def _missing_result_diagnostic(
        relation: DelegationRecord,
        run: RunRecord,
    ) -> Diagnostic:
        return Diagnostic(
            code="result_delivery_incomplete",
            severity="warning",
            component="application.agent_recovery",
            message="Terminal child Run had no DelegationResult; startup rebuilt it.",
            session_id=run.session_id,
            run_id=run.run_id,
            resource_id=relation.delegation_id,
            recoverable=False,
            metadata={
                "root_session_id": relation.root_session_id,
                "repair": "delegation_result_rebuilt",
            },
        )

    @staticmethod
    def _parent_wait_diagnostic(
        *,
        session: SessionRecord,
        run: RunRecord,
        wait_id: str,
        message: str,
    ) -> Diagnostic:
        return Diagnostic(
            code="parent_agent_wait_mismatch",
            severity="error",
            component="application.agent_recovery",
            message=message,
            session_id=session.session_id,
            run_id=run.run_id,
            resource_id=wait_id,
            recoverable=True,
            suggested_action="Inspect the retained parent ToolCall and child relations.",
        )

    def _watch(self, delegation_id: str, handle: RunHandle) -> None:
        watcher = asyncio.create_task(
            self._watch_completion(delegation_id, handle),
            name=f"ouro-agent-{delegation_id}",
        )
        self._watchers.add(watcher)
        watcher.add_done_callback(self._watchers.discard)

    async def _watch_completion(self, delegation_id: str, handle: RunHandle) -> None:
        try:
            status = await self.scheduler.wait(handle)
            if self._terminal_status(status.status):
                await self._finalize_result(delegation_id)
                self._completion_event(delegation_id).set()
        finally:
            if self._run_handles.get(delegation_id) == handle:
                self._run_handles.pop(delegation_id, None)

    async def _finalize_result(self, delegation_id: str) -> DelegationRecord:
        lock = self._result_locks.setdefault(delegation_id, asyncio.Lock())
        async with lock:
            relation = await self._finalize_result_serialized(delegation_id)
        return await self._deliver_result(relation)

    async def _finalize_result_serialized(
        self,
        delegation_id: str,
    ) -> DelegationRecord:
        relation = self._load_by_id(delegation_id)
        if relation.result is not None:
            return relation
        if not self.recovery.relation_is_consistent(relation):
            return relation
        if relation.child_session_id is None or relation.current_child_run_id is None:
            return relation
        snapshot = self.store.load_runtime_session(relation.child_session_id)
        run = next(
            (item for item in snapshot.runs if item.run_id == relation.current_child_run_id),
            None,
        )
        if run is None or run.status in ACTIVE_RUN_STATUSES:
            return relation
        messages = [item for item in snapshot.messages if item.run_id == run.run_id]
        first_input = next(
            (
                item
                for item in messages
                if item.role == "user"
                and item.metadata.get("session_input_source") == "delegation"
                and item.metadata.get("delegation_id") == relation.delegation_id
            ),
            None,
        )
        result_message = next(
            (item for item in reversed(messages) if item.role == "assistant"), None
        )
        policy = self._result_delivery_policy(
            relation,
            is_first_result=len(snapshot.runs) == 1,
        )
        result = DelegationResult(
            child_session_id=relation.child_session_id,
            child_run_id=run.run_id,
            terminal_status=cast(
                Literal["completed", "failed", "aborted", "paused", "recoverable"],
                run.status,
            ),
            result_message_id=(result_message.message_id if result_message is not None else None),
            resource_refs=tuple(
                dict.fromkeys(
                    resource
                    for message in messages
                    if message.role in {"assistant", "tool_result"}
                    for resource in message.resource_refs
                )
            ),
            usage=dict(run.usage),
            delivery=ResultDelivery(
                policy=policy,
                status=("skipped" if policy == "notify_only" else "pending"),
            ),
        )
        try:
            updated = self.store.save_delegation(
                relation.model_copy(
                    update={
                        "initial_message_id": (
                            first_input.message_id if first_input is not None else None
                        ),
                        "result": result,
                    }
                ),
                expected_revision=relation.revision,
            )
        except DelegationRevisionConflictError:
            updated = self._load_by_id(delegation_id)
            if (
                updated.result is None
                or updated.result.child_session_id != relation.child_session_id
                or updated.result.child_run_id != run.run_id
            ):
                raise
            return updated
        await self.events.publish(
            session_id=updated.root_session_id,
            run_id=updated.parent_run_id,
            event_type="agent_result_ready",
            payload={
                "delegation_id": updated.delegation_id,
                "child_session_id": updated.child_session_id,
                "child_run_id": updated.current_child_run_id,
                "terminal_status": run.status,
                "delivery_id": result.delivery.delivery_id,
            },
        )
        return updated

    def _result_delivery_policy(
        self,
        relation: DelegationRecord,
        *,
        is_first_result: bool,
    ) -> DeliveryPolicy:
        """Choose one result's delivery policy from the state at finalization time.

        The first result keeps the spawn-time contract: a synchronous SpawnAgent
        resolves its parked ToolCall. Results from later follow-up Runs have no
        waiter, so they push to the parent at the next safe boundary unless the
        parent Run is already aborted/failed.
        """

        if relation.delivery_policy != "resume_waiter" or is_first_result:
            return relation.delivery_policy
        parent_run = self.store.load_run(relation.parent_session_id, relation.parent_run_id)
        if parent_run.status in {"aborted", "failed"}:
            return "resume_waiter"
        return "next_boundary"

    async def _deliver_result(self, relation: DelegationRecord) -> DelegationRecord:
        """Durably hand one pending result to the parent Host input queue."""

        lock = self._result_locks.setdefault(relation.delegation_id, asyncio.Lock())
        async with lock:
            relation = self._load_by_id(relation.delegation_id)
            result = relation.result
            if result is None or result.delivery.status != "pending":
                return relation
            if result.delivery.policy in {"notify_only", "resume_waiter"}:
                return relation
            parent_resource_refs = self._copy_result_resources_to_parent(
                (relation,),
                parent_session_id=relation.parent_session_id,
            )
            existing_target = self._existing_delivery_target(relation)
            if existing_target is not None:
                return await self._record_delivery(relation, **existing_target)
        receipt = await self.scheduler.route_host_input(
            HostSessionInput(
                prompt=self._delivery_prompt(relation),
                mode="steer",
                host_source_id=f"ouro.subagents/results/{result.delivery.delivery_id}",
                semantic_tag="subagent-result",
                queue_id=result.delivery.delivery_id,
                resource_refs=parent_resource_refs,
            ),
            session_id=relation.parent_session_id,
        )
        async with lock:
            return await self._record_delivery(
                self._load_by_id(relation.delegation_id),
                target_queue_id=receipt.queue_id,
            )

    def _delivery_prompt(self, relation: DelegationRecord) -> str:
        assert relation.result is not None
        detail = self.read_agent(relation.delegation_id)
        return _subagent_result_content(
            self._handle(relation),
            relation,
            detail.final_text[:8_000],
        )

    def _existing_delivery_target(
        self,
        relation: DelegationRecord,
    ) -> dict[str, str] | None:
        assert relation.result is not None
        delivery_id = relation.result.delivery.delivery_id
        snapshot = self.store.load_runtime_session(relation.parent_session_id)
        host_source_id = f"ouro.subagents/results/{delivery_id}"
        for message in snapshot.messages:
            if message.metadata.get("host_source_id") == host_source_id:
                target = {"target_message_id": message.message_id}
                if message.run_id is not None:
                    target["target_run_id"] = message.run_id
                return target
        raw_queue = snapshot.session.metadata.get("follow_up_queue")
        if isinstance(raw_queue, list) and any(
            isinstance(item, dict) and item.get("queue_id") == delivery_id for item in raw_queue
        ):
            return {"target_queue_id": delivery_id}
        return None

    async def _record_delivery(
        self,
        relation: DelegationRecord,
        *,
        target_run_id: str | None = None,
        target_message_id: str | None = None,
        target_queue_id: str | None = None,
    ) -> DelegationRecord:
        if relation.result is None or relation.result.delivery.status == "delivered":
            return relation
        delivery = relation.result.delivery.model_copy(
            update={
                "status": "delivered",
                "target_run_id": target_run_id,
                "target_message_id": target_message_id,
                "target_queue_id": target_queue_id,
                "delivered_at": utc_now(),
            }
        )
        try:
            updated = self.store.save_delegation(
                relation.model_copy(
                    update={"result": relation.result.model_copy(update={"delivery": delivery})}
                ),
                expected_revision=relation.revision,
            )
        except DelegationRevisionConflictError:
            updated = self._load_by_id(relation.delegation_id)
            if (
                updated.result is None
                or updated.result.delivery.delivery_id != delivery.delivery_id
                or updated.result.delivery.status != "delivered"
            ):
                raise
            return updated
        await self.events.publish(
            session_id=updated.root_session_id,
            run_id=target_run_id or updated.parent_run_id,
            event_type="agent_result_delivered",
            payload={
                "delegation_id": updated.delegation_id,
                "delivery_id": delivery.delivery_id,
                "policy": delivery.policy,
                "target_run_id": target_run_id,
                "target_message_id": target_message_id,
                "target_queue_id": target_queue_id,
            },
        )
        return updated

    async def _await_agent_wait(
        self,
        *,
        session_id: str,
        run_id: str,
        tool_call_id: str,
        tool_name: str,
        wait: AgentWaitRequest,
    ) -> None:
        completion = self._completion_event(wait.delegation_id)
        while True:
            parent_run = self.store.load_run(session_id, run_id)
            if parent_run.status != "waiting_child":
                return
            relation = self._load_by_id(wait.delegation_id)
            if self._is_terminal(relation):
                break
            completion.clear()
            if self._is_terminal(self._load_by_id(wait.delegation_id)):
                break
            await completion.wait()

        resolution = AgentWaitResolution(
            resolved_delegation_id=wait.delegation_id,
            reason="condition_met",
        )
        await self._finalize_result(wait.delegation_id)
        relation = self._load_by_id(wait.delegation_id)
        resource_refs = self._copy_result_resources_to_parent(
            (relation,),
            parent_session_id=session_id,
        )
        result = self._spawn_tool_result(
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            resolution=resolution,
            resource_refs=resource_refs,
        )
        try:
            handle = await self.scheduler.resume_agent_wait(
                session_id=session_id,
                run_id=run_id,
                tool_call_id=tool_call_id,
                resolution=resolution,
                result=result,
            )
        except (FileNotFoundError, SessionBusyError, ValueError):
            return
        await self.scheduler.wait(handle)
        if self._tool_result_is_committed(
            session_id=session_id,
            run_id=run_id,
            tool_call_id=tool_call_id,
        ):
            await self._mark_waiter_delivery(wait.delegation_id, target_run_id=run_id)

    def _spawn_tool_result(
        self,
        *,
        tool_call_id: str,
        tool_name: str,
        resolution: AgentWaitResolution,
        resource_refs: tuple[str, ...],
    ) -> ToolResult:
        if resolution.resolved_delegation_id is None:
            raise ValueError("synchronous SpawnAgent has no resolved subagent")
        relation = self._load_by_id(resolution.resolved_delegation_id)
        if relation.result is None:
            raise ValueError("synchronous SpawnAgent subagent has no terminal result")
        detail = self.read_agent(relation.delegation_id)
        handle = self._handle(relation)
        structured = {
            "handle": handle.model_dump(mode="json"),
            "result": {
                "terminal_status": relation.result.terminal_status,
                "result_message_id": relation.result.result_message_id,
                "resource_refs": list(relation.result.resource_refs),
                "usage": relation.result.usage,
                "final_text": detail.final_text[:8_000],
            },
        }
        return ToolResult(
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            is_error=relation.result.terminal_status != "completed",
            content=_subagent_result_content(handle, relation, detail.final_text[:8_000]),
            structured=structured,
            resource_refs=list(resource_refs),
            ended_at=utc_now(),
        )

    def _tool_result_is_committed(
        self,
        *,
        session_id: str,
        run_id: str,
        tool_call_id: str,
    ) -> bool:
        snapshot = self.store.load_runtime_session(session_id)
        return any(
            message.run_id == run_id
            and message.tool_result is not None
            and message.tool_result.tool_call_id == tool_call_id
            for message in snapshot.messages
        )

    def _copy_result_resources_to_parent(
        self,
        relations: Iterable[DelegationRecord],
        *,
        parent_session_id: str,
    ) -> tuple[str, ...]:
        materialized = tuple(relations)
        resource_refs = tuple(
            dict.fromkeys(
                resource_id
                for relation in materialized
                if relation.result is not None
                for resource_id in relation.result.resource_refs
            )
        )
        if not resource_refs:
            return ()
        if len(resource_refs) > 16:
            raise ValueError("Agent results exceed the parent model attachment limit")
        if self.resources is None:
            raise ValueError("Agent result resources require ResourceService")
        for relation in materialized:
            if relation.parent_session_id != parent_session_id:
                raise ValueError("Agent result does not belong to the target parent Session")
            if relation.result is None or not relation.result.resource_refs:
                continue
            if relation.child_session_id is None:
                raise ValueError("Agent result has no child Session identity")
            self.resources.copy_references(
                relation.child_session_id,
                parent_session_id,
                relation.result.resource_refs,
            )
        return resource_refs

    def _require_parent(
        self,
        delegation_id: str,
        parent_session_id: str,
    ) -> DelegationRecord:
        relation = self._load_by_id(delegation_id)
        if relation.parent_session_id != parent_session_id:
            raise ValueError("delegation is not a direct child of this parent")
        return relation

    @staticmethod
    def _extension_metadata(run: RunRecord) -> dict[str, object]:
        return {
            key: run.metadata[key]
            for key in ("extension_catalog_id", "extension_generations")
            if key in run.metadata
        }

    def _require_child_identity(
        self,
        delegation_id: str,
        *,
        child_session_id: str,
        child_run_id: str,
    ) -> DelegationRecord:
        relation = self._load_by_id(delegation_id)
        if (
            relation.child_session_id != child_session_id
            or relation.current_child_run_id != child_run_id
        ):
            raise ValueError("child identity does not match the Delegation")
        return relation

    async def _mark_waiter_delivery(
        self,
        delegation_id: str,
        *,
        target_run_id: str,
    ) -> None:
        relation = self._load_by_id(delegation_id)
        if relation.result is None:
            return
        delivery = relation.result.delivery
        if delivery.policy != "resume_waiter" or delivery.status == "delivered":
            return
        await self._record_delivery(relation, target_run_id=target_run_id)

    def _resolve_interrupted_parent_wait(self, session_id: str, run_id: str) -> None:
        try:
            run = self.store.load_run(session_id, run_id)
        except FileNotFoundError:
            return
        batch = run.active_tool_batch
        if run.status not in {"aborted", "failed"} or batch is None:
            return
        changed = False
        calls = []
        interrupted_delegation_ids: list[str] = []
        for call in batch.calls:
            if call.status != "waiting_child" or call.agent_wait is None:
                calls.append(call)
                continue
            delegation_id = call.agent_wait.delegation_id
            resolved_id = (
                delegation_id if self._is_terminal(self._load_by_id(delegation_id)) else None
            )
            resolution = AgentWaitResolution(
                resolved_delegation_id=resolved_id,
                reason="interrupted",
            )
            calls.append(
                call.model_copy(
                    update={
                        "status": "aborted",
                        "agent_wait": call.agent_wait.model_copy(
                            update={"state": "resolved", "resolution": resolution}
                        ),
                        "result": ToolResult(
                            tool_call_id=call.request.id,
                            tool_name=call.request.name,
                            is_error=True,
                            content="Parent Run terminated while waiting for subagents.",
                            structured={"interrupted": True},
                            ended_at=utc_now(),
                        ),
                    }
                )
            )
            changed = True
            interrupted_delegation_ids.append(delegation_id)
        if changed:
            self.store.save_run(
                run.model_copy(
                    update={
                        "active_tool_batch": batch.model_copy(
                            update={"calls": calls, "updated_at": utc_now()}
                        )
                    }
                )
            )
            for delegation_id in interrupted_delegation_ids:
                self._completion_event(delegation_id).set()

    def _resolve_parent_catalog(
        self,
        parent_run: RunRecord,
        agent_id: str,
    ) -> AgentCatalogEntry:
        if parent_run.agent_catalog is None:
            raise ValueError("parent Run has no frozen Agent catalog")
        return parent_run.agent_catalog.resolve(agent_id)

    def _effective_child_budget(
        self,
        *,
        root_session_id: str,
        parent_run: RunRecord,
        requested: RunBudgetPolicy,
    ) -> RunBudgetPolicy:
        state = self._root_budget_state(root_session_id, parent_run)
        if state is None:
            return requested

        def remaining_int(limit: int | None, used: int, name: str) -> int | None:
            if limit is None:
                return None
            remaining = limit - used
            if remaining < 1:
                raise SessionBusyError(f"root {name} budget is exhausted")
            return remaining

        remaining_elapsed: float | None = None
        if state.policy.max_elapsed_seconds is not None:
            remaining_elapsed = state.policy.max_elapsed_seconds - state.elapsed_seconds
            if remaining_elapsed <= 0:
                raise SessionBusyError("root elapsed time budget is exhausted")

        remaining = RunBudgetPolicy(
            max_model_rounds=remaining_int(
                state.policy.max_model_rounds,
                state.model_rounds,
                "model round",
            ),
            max_input_tokens=remaining_int(
                state.policy.max_input_tokens,
                state.input_tokens,
                "input token",
            ),
            max_output_tokens=remaining_int(
                state.policy.max_output_tokens,
                state.output_tokens,
                "output token",
            ),
            max_elapsed_seconds=remaining_elapsed,
        )
        return requested.constrained_by(remaining)

    def budget_reason(
        self,
        run: RunRecord,
        next_model_round: int,
    ) -> RunBudgetReason | None:
        """Recheck a shared root budget at each ordinary model boundary."""

        del next_model_round
        session = self.store.load_session_record(run.session_id)
        if session.session_kind == "subagent":
            root_session_id = session.root_session_id
            if root_session_id is None:
                raise PersistenceError(f"subagent Session {session.session_id} has no root Session")
            relation = self.store.find_delegation_by_child_session(
                root_session_id,
                run.session_id,
            )
            if relation is None:
                raise PersistenceError(
                    f"subagent Session {session.session_id} has no delegation relation"
                )
            parent_run = self.store.load_run(
                relation.parent_session_id,
                relation.parent_run_id,
            )
        else:
            parent_run = self.store.load_run(run.session_id, run.run_id)
            root_session_id = session.root_session_id or session.session_id
        state = self._root_budget_state(root_session_id, parent_run)
        if state is None:
            return None
        return state.policy.exceeded_reason(
            next_model_round=state.model_rounds + 1,
            input_tokens=state.input_tokens,
            output_tokens=state.output_tokens,
            elapsed_seconds=state.elapsed_seconds,
        )

    def _root_budget_state(
        self,
        root_session_id: str,
        parent_run: RunRecord,
    ) -> _RootBudgetState | None:
        raw = parent_run.metadata.get("run_budget")
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise ValueError("parent Run has invalid budget metadata")
        policy = RunBudgetPolicy.model_validate(raw)
        latest_parent = self.store.load_run(parent_run.session_id, parent_run.run_id)
        input_tokens = int(latest_parent.usage.get("input_tokens", 0))
        output_tokens = int(latest_parent.usage.get("output_tokens", 0))
        model_rounds = latest_parent.model_round_count
        seen_runs = {(latest_parent.session_id, latest_parent.run_id)}
        for relation in self.store.list_delegations(root_session_id):
            if relation.parent_run_id != parent_run.run_id or relation.child_session_id is None:
                continue
            for child_run in self.store.load_runs(relation.child_session_id):
                identity = (child_run.session_id, child_run.run_id)
                if identity in seen_runs:
                    continue
                seen_runs.add(identity)
                input_tokens += int(child_run.usage.get("input_tokens", 0))
                output_tokens += int(child_run.usage.get("output_tokens", 0))
                model_rounds += child_run.model_round_count
        return _RootBudgetState(
            policy=policy,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model_rounds=model_rounds,
            elapsed_seconds=max(
                0.0,
                (utc_now() - latest_parent.started_at).total_seconds(),
            ),
        )

    def _handle(self, relation: DelegationRecord) -> AgentHandleView:
        return AgentHandleView(
            revision=relation.revision,
            delegation_id=relation.delegation_id,
            child_session_id=relation.child_session_id,
            child_run_id=relation.current_child_run_id,
            agent_id=relation.agent_id,
            agent_name=relation.definition.name,
            nickname=relation.nickname,
            status=self._status(relation),
            execution_mode=relation.execution_mode,
            control_state=relation.control_state,
        )

    def _status(self, relation: DelegationRecord) -> str:
        return delegation_status(self.store, relation)

    def _is_terminal(self, relation: DelegationRecord) -> bool:
        return self._terminal_status(self._status(relation))

    def _child_terminal(self, relation: DelegationRecord) -> bool:
        return self._is_terminal(relation)

    @staticmethod
    def _terminal_status(status: str) -> bool:
        return status in {
            "completed",
            "failed",
            "aborted",
            "paused",
            "recoverable",
            "closed",
        }

    def _load_by_id(self, delegation_id: str) -> DelegationRecord:
        return self.store.load_delegation_by_id(delegation_id)

    def _all_relations(self) -> list[DelegationRecord]:
        relations: list[DelegationRecord] = []
        roots = {
            session.root_session_id or session.session_id
            for session in self.store.list_sessions()
            if session.session_kind == "primary"
        }
        for root_id in sorted(roots):
            try:
                relations.extend(self.store.list_delegations(root_id))
            except PersistenceError:
                continue
        return relations

    def _completion_event(self, delegation_id: str) -> asyncio.Event:
        return self._completion_events.setdefault(delegation_id, asyncio.Event())

    def _wait_finished(self, task: asyncio.Task[None], wait_id: str) -> None:
        self._watchers.discard(task)
        self._armed_wait_ids.discard(wait_id)


def _async_spawn_content(handle: AgentHandleView) -> str:
    """Render the model-facing Markdown result of an asynchronous SpawnAgent call."""

    return (
        "## Subagent Dispatched (Async)\n\n"
        f"- **Agent**: {handle.agent_name} (`{handle.agent_id}`)\n"
        f"- **Delegation ID**: `{handle.delegation_id}`\n"
        f"- **Status**: {handle.status}\n\n"
        "The subagent is now running in the background. It will proactively push "
        "a notification with its result when it completes; do not poll for its status."
    )


def _subagent_result_content(
    handle: AgentHandleView,
    relation: DelegationRecord,
    final_text: str,
) -> str:
    """Render the model-facing Markdown result shared by sync SpawnAgent results
    and async subagent result deliveries."""

    assert relation.result is not None
    lines = [
        "## Subagent Result",
        "",
        f"- **Agent**: {handle.agent_name} (`{handle.agent_id}`)",
        f"- **Delegation ID**: `{handle.delegation_id}`",
        f"- **Status**: {relation.result.terminal_status}",
    ]
    if relation.result.resource_refs:
        refs = ", ".join(f"`{ref}`" for ref in relation.result.resource_refs)
        lines.append(f"- **Resources**: {refs}")
    lines.extend(["", "### Final Output", "", final_text or "(no final output)"])
    return "\n".join(lines)


def _send_agent_message_content(handle: AgentHandleView) -> str:
    """Render the model-facing Markdown result of a SendAgentMessage call."""

    return (
        "## Message Sent to Subagent\n\n"
        f"- **Agent**: {handle.agent_name} (`{handle.agent_id}`)\n"
        f"- **Delegation ID**: `{handle.delegation_id}`\n"
        f"- **Status**: {handle.status}\n\n"
        "Your message has been delivered to the subagent. It will proactively push "
        "a notification with its result when it completes; do not poll for its status."
    )


__all__ = ["AgentService", "RunScheduler"]
