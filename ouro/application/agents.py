from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Iterable
from datetime import timedelta
from functools import partial
from typing import Literal, Protocol, cast

from pydantic import JsonValue

from ouro.application.agent_definitions import AgentDefinitionService
from ouro.application.agent_models import (
    AgentHandleView,
    AgentInteractionView,
    AgentReadView,
    AgentTreeNode,
    AgentTreeSnapshot,
    AgentWaitResultView,
    SpawnAgentRequest,
)
from ouro.application.supervisor_models import (
    RunHandle,
    RuntimeActivitySnapshot,
    RuntimeStatusSnapshot,
    SupervisorStartResult,
)
from ouro.core.agents import (
    AgentCatalogEntry,
    AgentControlRequest,
    AgentControlResult,
    AgentWaitRequest,
    AgentWaitResolution,
    DelegatedConstraints,
    DelegationRecord,
    DelegationResult,
    ResultDelivery,
)
from ouro.core.base import utc_now
from ouro.core.errors import SessionBusyError
from ouro.core.events import EventRecord
from ouro.core.run_budget import RunBudgetPolicy
from ouro.core.session_controls import HostSessionInput
from ouro.core.state import ACTIVE_RUN_STATUSES, RunRecord, SessionRecord
from ouro.core.tool_batches import ToolCallBatchRecord
from ouro.core.tools import ToolInvocation, ToolResult
from ouro.sessions import SessionStore


class RunScheduler(Protocol):
    async def start(self) -> SupervisorStartResult: ...

    async def submit_prepared(
        self, run: RunRecord, session_input: HostSessionInput
    ) -> RunHandle: ...

    async def wait(self, handle: RunHandle) -> RuntimeStatusSnapshot: ...

    async def abort(
        self, handle_or_session: RunHandle | str, *, reason: str = "User aborted"
    ) -> RuntimeStatusSnapshot: ...

    def activity(self, handle_or_session: RunHandle | str) -> RuntimeActivitySnapshot: ...

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
    ) -> RunHandle | None: ...

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


class AgentService:
    """Orchestrate durable Delegations over ordinary Session and Run records."""

    def __init__(
        self,
        *,
        store: SessionStore,
        definitions: AgentDefinitionService,
        scheduler: RunScheduler,
        max_active_children_per_root: int = 4,
    ) -> None:
        if max_active_children_per_root < 1:
            raise ValueError("max_active_children_per_root must be positive")
        self.store = store
        self.definitions = definitions
        self.scheduler = scheduler
        self.max_active_children_per_root = max_active_children_per_root
        self._run_handles: dict[str, RunHandle] = {}
        self._completion_events: dict[str, asyncio.Event] = {}
        self._watchers: set[asyncio.Task[None]] = set()
        self._armed_wait_ids: set[str] = set()
        self._closed = False

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
                    delivery_policy=request.delivery_policy,
                )
            )
            return AgentControlResult(
                content=json.dumps(handle.model_dump(mode="json"), ensure_ascii=False),
                structured={"handle": handle.model_dump(mode="json")},
            )
        if request.action == "wait":
            return self._prepare_wait(request, session=session, run=run)
        if request.action == "list":
            return self._list_control(session)
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
                content=json.dumps(handle.model_dump(mode="json"), ensure_ascii=False),
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
        if wait.wait_id in self._armed_wait_ids:
            return
        self._armed_wait_ids.add(wait.wait_id)
        waiter = asyncio.create_task(
            self._await_agent_wait(
                session_id=session.session_id,
                run_id=run.run_id,
                tool_call_id=tool_call_id,
                wait=wait,
            ),
            name=f"ouro-agent-wait-{wait.wait_id}",
        )
        self._watchers.add(waiter)
        waiter.add_done_callback(partial(self._wait_finished, wait_id=wait.wait_id))

    async def start(self) -> tuple[str, ...]:
        await self.scheduler.start()
        return await self.reconcile()

    async def close(self) -> None:
        self._closed = True
        if not self._watchers:
            return
        for watcher in tuple(self._watchers):
            watcher.cancel()
        await asyncio.gather(*self._watchers, return_exceptions=True)
        self._watchers.clear()

    async def on_runtime_event(self, event: EventRecord) -> None:
        """Cascade terminal parent failures through bound child relations."""

        if event.event_type not in {"run_aborted", "run_failed"} or event.run_id is None:
            return
        self._resolve_interrupted_parent_wait(event.session_id, event.run_id)
        relations = [
            relation
            for relation in self._all_relations()
            if relation.parent_run_id == event.run_id
            and relation.lifecycle_binding == "bound"
            and not self._is_terminal(relation)
        ]
        for relation in relations:
            if relation.child_session_id is None:
                continue
            try:
                await self.scheduler.abort(
                    self._run_handles.get(relation.delegation_id) or relation.child_session_id,
                    reason=f"Bound parent Run {event.run_id} terminated",
                )
                await self._finalize_result(relation.delegation_id)
            except (FileNotFoundError, KeyError, SessionBusyError, ValueError):
                continue

    async def spawn_agent(self, request: SpawnAgentRequest) -> AgentHandleView:
        if self._closed:
            raise RuntimeError("AgentService is closed")
        await self.scheduler.start()
        parent_session = self.store.load_session_record(request.parent_session_id)
        parent_run = self.store.load_run(request.parent_session_id, request.parent_run_id)
        if parent_run.status not in ACTIVE_RUN_STATUSES:
            raise SessionBusyError(
                f"parent run is not active: {parent_run.run_id} ({parent_run.status})"
            )
        if parent_session.active_run_id != parent_run.run_id:
            raise SessionBusyError("parent Session does not project the requested active Run")
        if parent_session.session_kind == "subagent":
            raise ValueError("maximum subagent depth is 1")
        if request.lifecycle_binding != "bound":
            raise ValueError("detached child is not supported in the first release")
        existing = self.store.find_delegation_by_idempotency_key(
            parent_session.root_session_id or parent_session.session_id,
            request.request_id,
        )
        if existing is not None:
            return self._handle(existing)

        entry = self._resolve_parent_catalog(parent_run, request.agent_id)
        root_id = parent_session.root_session_id or parent_session.session_id
        self._require_root_quota(root_id, entry=entry)
        budget = self._effective_child_budget(
            root_session_id=root_id,
            parent_run=parent_run,
            requested=entry.definition.default_budget,
        )
        delegation = DelegationRecord(
            idempotency_key=request.request_id,
            root_session_id=root_id,
            parent_session_id=parent_session.session_id,
            parent_run_id=parent_run.run_id,
            parent_tool_call_id=request.parent_tool_call_id,
            agent_id=entry.definition.agent_id,
            nickname=request.nickname,
            task_description=request.description,
            prompt_digest="sha256:" + hashlib.sha256(request.task.encode("utf-8")).hexdigest(),
            execution_mode=request.execution_mode,
            lifecycle_binding=request.lifecycle_binding,
            delivery_policy=request.delivery_policy,
            depth=1,
            definition=entry.definition,
            effective_tools=entry.effective_tools,
            constraints=DelegatedConstraints(
                allowed_tool_names=tuple(tool.public_name for tool in entry.effective_tools),
                workspace=parent_session.workspace,
                allow_write=any(tool.effect == "state_changing" for tool in entry.effective_tools),
                allow_mcp=any(tool.source == "mcp" for tool in entry.effective_tools),
            ),
            approval_mode=parent_session.approval_mode,
            interaction_mode=parent_session.interaction_mode,
            budget=budget,
        )
        delegation = self.store.create_delegation(delegation)
        child = SessionRecord(
            session_kind="subagent",
            root_session_id=root_id,
            parent_session_id=parent_session.session_id,
            delegation_id=delegation.delegation_id,
            title=request.nickname or entry.definition.name,
            workspace=parent_session.workspace,
            workspace_trusted=parent_session.workspace_trusted,
            current_model=entry.definition.model or parent_session.current_model,
            approval_mode=parent_session.approval_mode,
            interaction_mode=parent_session.interaction_mode,
        )
        self.store.create_session(child)
        delegation = self.store.save_delegation(
            delegation.model_copy(
                update={
                    "child_session_id": child.session_id,
                    "spawn_state": "child_created",
                }
            ),
            expected_revision=delegation.revision,
        )
        run = RunRecord(
            session_id=child.session_id,
            status="queued",
            origin="host",
            parent_run_id=parent_run.run_id,
            initiator_id=f"ouro.subagents/{delegation.delegation_id}",
            trigger="subagent",
            model=(entry.definition.model or parent_run.model),
            metadata={
                **self._extension_metadata(parent_run),
                "delegation_id": delegation.delegation_id,
                "root_session_id": root_id,
                "agent_id": entry.definition.agent_id,
                "agent_definition_contract_hash": entry.definition.contract_hash,
                "effective_tool_names": [tool.public_name for tool in entry.effective_tools],
                "effective_tool_refs": [
                    tool.model_dump(mode="json") for tool in entry.effective_tools
                ],
                "agent_system_prompt": entry.definition.system_prompt,
                "run_budget": budget.model_dump(mode="json"),
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
        delegation = self.store.save_delegation(
            delegation.model_copy(update={"current_child_run_id": run.run_id}),
            expected_revision=delegation.revision,
        )
        host_input = HostSessionInput(
            prompt=request.task,
            mode="follow_up",
            title=request.nickname or entry.definition.name,
            host_source_id=f"ouro.subagents/{delegation.delegation_id}",
        )
        try:
            handle = await self.scheduler.submit_prepared(run, host_input)
        except BaseException as exc:
            latest = self._load_by_id(delegation.delegation_id)
            self.store.save_delegation(
                latest.model_copy(
                    update={
                        "spawn_state": "failed",
                        "error": f"child submission failed: {type(exc).__name__}",
                    }
                ),
                expected_revision=latest.revision,
            )
            try:
                await self.scheduler.abort(
                    child.session_id,
                    reason="Child submission failed",
                )
            except Exception:  # noqa: BLE001 - preserve the original submission error.
                pass
            self._completion_event(delegation.delegation_id).set()
            raise
        delegation = self.store.save_delegation(
            delegation.model_copy(update={"spawn_state": "submitted"}),
            expected_revision=delegation.revision,
        )
        self._run_handles[delegation.delegation_id] = handle
        self.store.append_ordered_event(
            EventRecord(
                session_id=root_id,
                run_id=parent_run.run_id,
                event_type="agent_spawned",
                payload={
                    "delegation_id": delegation.delegation_id,
                    "child_session_id": child.session_id,
                    "child_run_id": run.run_id,
                    "agent_id": delegation.agent_id,
                    "execution_mode": delegation.execution_mode,
                },
            )
        )
        self._watch(delegation.delegation_id, handle)
        return self._handle(delegation)

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
        relation = self._load_by_id(delegation_id)
        if relation.child_session_id is None:
            raise ValueError("delegation has no child Session")
        handle = self._run_handles.get(delegation_id)
        await self.scheduler.abort(handle or relation.child_session_id, reason=reason)
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
            ),
        )
        self._run_handles[delegation_id] = handle
        self._completion_event(delegation_id).clear()
        self._watch(delegation_id, handle)
        return self._handle(relation)

    def close_agent(self, delegation_id: str) -> AgentHandleView:
        relation = self._load_by_id(delegation_id)
        if not self._is_terminal(relation):
            raise SessionBusyError("active child must be interrupted before close")
        if relation.control_state == "closed":
            return self._handle(relation)
        updated = self.store.save_delegation(
            relation.model_copy(update={"control_state": "closed"}),
            expected_revision=relation.revision,
        )
        self.store.append_ordered_event(
            EventRecord(
                session_id=updated.root_session_id,
                run_id=updated.parent_run_id,
                event_type="agent_closed",
                payload={"delegation_id": updated.delegation_id},
            )
        )
        return self._handle(updated)

    def agent_tree_snapshot(self, root_session_id: str) -> AgentTreeSnapshot:
        relations = self.store.list_delegations(root_session_id)
        nodes: list[AgentTreeNode] = []
        active_count = 0
        waiting_count = 0
        terminal_count = 0
        for relation in relations:
            if relation.child_session_id is None:
                continue
            status = self._status(relation)
            if self._terminal_status(status):
                terminal_count += 1
            elif status.startswith("waiting"):
                waiting_count += 1
            else:
                active_count += 1
            activity: RuntimeActivitySnapshot | None
            try:
                activity = self.scheduler.activity(relation.child_session_id)
            except (FileNotFoundError, KeyError):
                activity = None
            nodes.append(
                AgentTreeNode(
                    delegation_id=relation.delegation_id,
                    session_id=relation.child_session_id,
                    parent_session_id=relation.parent_session_id,
                    agent_id=relation.agent_id,
                    agent_name=relation.definition.name,
                    nickname=relation.nickname,
                    task_description=relation.task_description,
                    status=status,
                    execution_mode=relation.execution_mode,
                    depth=relation.depth,
                    activity=activity,
                )
            )
        return AgentTreeSnapshot(
            root_session_id=root_session_id,
            revision=sum(item.revision + 1 for item in relations),
            nodes=tuple(nodes),
            active_count=active_count,
            waiting_count=waiting_count,
            terminal_count=terminal_count,
        )

    def list_agent_interactions(
        self,
        root_session_id: str,
    ) -> tuple[AgentInteractionView, ...]:
        interactions: list[AgentInteractionView] = []
        for relation in self.store.list_delegations(root_session_id):
            if relation.child_session_id is None or relation.current_child_run_id is None:
                continue
            run = self.store.load_run(relation.child_session_id, relation.current_child_run_id)
            batch = run.active_tool_batch
            if batch is None:
                continue
            for call in batch.calls:
                if call.status == "awaiting_approval" and call.approval is not None:
                    interactions.append(
                        AgentInteractionView(
                            kind="approval",
                            delegation_id=relation.delegation_id,
                            agent_id=relation.agent_id,
                            agent_name=relation.definition.name,
                            task_description=relation.task_description,
                            child_session_id=relation.child_session_id,
                            child_run_id=run.run_id,
                            tool_call_id=call.request.id,
                            tool_name=call.request.name,
                            request_id=call.approval.approval_id,
                            requested_at=call.approval.requested_at.isoformat(),
                        )
                    )
                if call.status == "waiting_user" and call.input_request is not None:
                    interactions.append(
                        AgentInteractionView(
                            kind="user_input",
                            delegation_id=relation.delegation_id,
                            agent_id=relation.agent_id,
                            agent_name=relation.definition.name,
                            task_description=relation.task_description,
                            child_session_id=relation.child_session_id,
                            child_run_id=run.run_id,
                            tool_call_id=call.request.id,
                            tool_name=call.request.name,
                            request_id=f"{batch.batch_id}/{call.request.id}",
                            requested_at=batch.updated_at.isoformat(),
                        )
                    )
        return tuple(interactions)

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

    async def reconcile(self) -> tuple[str, ...]:
        diagnostics: list[str] = []
        for relation in self.store.load_incomplete_delegations():
            if relation.child_session_id is None:
                failed = relation.model_copy(
                    update={
                        "spawn_state": "failed",
                        "error": "delegation_child_missing",
                    }
                )
                self.store.save_delegation(failed, expected_revision=relation.revision)
                diagnostics.append("delegation_child_missing")
                continue
            if relation.current_child_run_id is None:
                child_runs = self.store.load_runs(relation.child_session_id)
                if not child_runs:
                    failed = relation.model_copy(
                        update={
                            "spawn_state": "failed",
                            "error": "delegation_run_missing",
                        }
                    )
                    self.store.save_delegation(failed, expected_revision=relation.revision)
                    diagnostics.append("delegation_run_missing")
                    continue
                relation = self.store.save_delegation(
                    relation.model_copy(update={"current_child_run_id": child_runs[-1].run_id}),
                    expected_revision=relation.revision,
                )
            if relation.spawn_state == "child_created":
                self.store.save_delegation(
                    relation.model_copy(update={"spawn_state": "submitted"}),
                    expected_revision=relation.revision,
                )
        for relation in self._all_relations():
            if relation.result is None and self._child_terminal(relation):
                await self._finalize_result(relation.delegation_id)
        for relation in self.store.load_pending_delegation_deliveries():
            await self._deliver_result(relation)
        for session in self.store.list_sessions():
            if session.session_kind != "primary" or session.active_run_id is None:
                continue
            run = self.store.load_run(session.session_id, session.active_run_id)
            batch = run.active_tool_batch
            if run.status != "waiting_child" or batch is None:
                continue
            for call in batch.calls:
                if call.status == "waiting_child" and call.agent_wait is not None:
                    await self.wait_parked(
                        session=session,
                        run=run,
                        batch=batch,
                        tool_call_id=call.request.id,
                        wait=call.agent_wait,
                    )
        return tuple(diagnostics)

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
        relation = self._load_by_id(delegation_id)
        if relation.result is not None:
            return await self._deliver_result(relation)
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
        first_input = next((item for item in messages if item.role == "system_synthetic"), None)
        result_message = next(
            (item for item in reversed(messages) if item.role == "assistant"), None
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
                    resource for message in messages for resource in message.resource_refs
                )
            ),
            usage=dict(run.usage),
            delivery=ResultDelivery(
                policy=relation.delivery_policy,
                status=("skipped" if relation.delivery_policy == "notify_only" else "pending"),
            ),
        )
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
        self.store.append_ordered_event(
            EventRecord(
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
        )
        return await self._deliver_result(updated)

    async def _deliver_result(self, relation: DelegationRecord) -> DelegationRecord:
        result = relation.result
        if result is None or result.delivery.status != "pending":
            return relation
        policy = result.delivery.policy
        if policy in {"notify_only", "resume_waiter"}:
            return relation
        existing_target = self._existing_delivery_target(relation)
        if existing_target is not None:
            return self._record_delivery(relation, **existing_target)

        host_source_id = f"ouro.subagents/results/{result.delivery.delivery_id}"
        mode: Literal["steer", "follow_up"] = "steer" if policy == "next_boundary" else "follow_up"
        handle = await self.scheduler.route_host_input(
            HostSessionInput(
                prompt=self._delivery_prompt(relation),
                mode=mode,
                host_source_id=host_source_id,
                queue_id=result.delivery.delivery_id,
            ),
            session_id=relation.parent_session_id,
        )
        if handle is None:
            return self._record_delivery(
                self._load_by_id(relation.delegation_id),
                target_queue_id=result.delivery.delivery_id,
            )
        settled = await self.scheduler.wait(handle)
        if settled.status == "failed" or settled.run_id is None:
            return relation
        return self._record_delivery(
            self._load_by_id(relation.delegation_id),
            target_run_id=settled.run_id,
        )

    def _delivery_prompt(self, relation: DelegationRecord) -> str:
        assert relation.result is not None
        detail = self.read_agent(relation.delegation_id)
        payload = {
            "kind": "agent_result",
            "delegation_id": relation.delegation_id,
            "agent_id": relation.agent_id,
            "agent_name": relation.definition.name,
            "child_session_id": relation.child_session_id,
            "child_run_id": relation.current_child_run_id,
            "terminal_status": relation.result.terminal_status,
            "result_message_id": relation.result.result_message_id,
            "resource_refs": list(relation.result.resource_refs),
            "final_text": detail.final_text[:8_000],
        }
        return json.dumps(payload, ensure_ascii=False)

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

    def _record_delivery(
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
        updated = self.store.save_delegation(
            relation.model_copy(
                update={"result": relation.result.model_copy(update={"delivery": delivery})}
            ),
            expected_revision=relation.revision,
        )
        self.store.append_ordered_event(
            EventRecord(
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
        )
        return updated

    def _prepare_wait(
        self,
        request: AgentControlRequest,
        *,
        session: SessionRecord,
        run: RunRecord,
    ) -> AgentControlResult:
        relations = tuple(
            self._require_parent(delegation_id, session.session_id)
            for delegation_id in request.delegation_ids
        )
        terminal_ids = tuple(
            relation.delegation_id for relation in relations if self._is_terminal(relation)
        )
        condition_met = (
            len(terminal_ids) == len(relations)
            if request.completion_condition == "all"
            else bool(terminal_ids)
        )
        if condition_met:
            return self._wait_result(
                request.delegation_ids,
                reason="condition_met",
                wait_id=None,
            )
        if request.timeout_seconds == 0:
            return self._wait_result(
                request.delegation_ids,
                reason="timeout",
                wait_id=None,
            )
        deadline = (
            utc_now() + timedelta(seconds=request.timeout_seconds)
            if request.timeout_seconds is not None
            else None
        )
        wait = AgentWaitRequest(
            delegation_ids=request.delegation_ids,
            completion_condition=request.completion_condition,
            deadline=deadline,
        )
        return AgentControlResult(status="waiting_child", wait=wait)

    def _list_control(self, session: SessionRecord) -> AgentControlResult:
        items: list[dict[str, object]] = []
        for relation in self.store.list_delegations(
            session.root_session_id or session.session_id,
            parent_session_id=session.session_id,
        ):
            item: dict[str, object] = {
                "handle": self._handle(relation).model_dump(mode="json"),
                "task_description": relation.task_description,
            }
            if relation.result is not None:
                detail = self.read_agent(relation.delegation_id)
                item["result"] = {
                    "terminal_status": relation.result.terminal_status,
                    "result_message_id": relation.result.result_message_id,
                    "resource_refs": list(relation.result.resource_refs),
                    "delivery_status": relation.result.delivery.status,
                    "final_text": detail.final_text[:8_000],
                }
            items.append(item)
        structured = {"agents": items[:32], "count": len(items)}
        return AgentControlResult(
            content=json.dumps(structured, ensure_ascii=False),
            structured=structured,
        )

    async def _await_agent_wait(
        self,
        *,
        session_id: str,
        run_id: str,
        tool_call_id: str,
        wait: AgentWaitRequest,
    ) -> None:
        reason: Literal["condition_met", "timeout", "interrupted"]
        while True:
            relations = tuple(self._load_by_id(item) for item in wait.delegation_ids)
            terminal_ids = tuple(
                relation.delegation_id for relation in relations if self._is_terminal(relation)
            )
            condition_met = (
                len(terminal_ids) == len(relations)
                if wait.completion_condition == "all"
                else bool(terminal_ids)
            )
            if condition_met:
                reason = "condition_met"
                break
            timeout: float | None = None
            if wait.deadline is not None:
                timeout = max(0.0, (wait.deadline - utc_now()).total_seconds())
                if timeout == 0:
                    reason = "timeout"
                    break
            pending_events = [
                self._completion_event(relation.delegation_id)
                for relation in relations
                if not self._is_terminal(relation)
            ]
            if not pending_events:
                reason = "interrupted"
                break
            event_tasks = [asyncio.create_task(event.wait()) for event in pending_events]
            try:
                done, _ = await asyncio.wait(
                    event_tasks,
                    timeout=timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                for task in event_tasks:
                    task.cancel()
                await asyncio.gather(*event_tasks, return_exceptions=True)
            if not done:
                reason = "timeout"
                break

        resolved_ids = tuple(
            delegation_id
            for delegation_id in wait.delegation_ids
            if self._is_terminal(self._load_by_id(delegation_id))
        )
        resolution = AgentWaitResolution(
            resolved_delegation_ids=resolved_ids,
            reason=reason,
        )
        result = self._wait_tool_result(
            tool_call_id=tool_call_id,
            wait=wait,
            resolution=resolution,
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
        resumed = await self.scheduler.wait(handle)
        if resumed.status != "failed":
            for delegation_id in resolution.resolved_delegation_ids:
                self._mark_waiter_delivery(delegation_id, target_run_id=run_id)

    def _wait_result(
        self,
        delegation_ids: tuple[str, ...],
        *,
        reason: Literal["condition_met", "timeout"],
        wait_id: str | None,
    ) -> AgentControlResult:
        structured = self._wait_structured(delegation_ids, reason=reason, wait_id=wait_id)
        return AgentControlResult(
            content=json.dumps(structured, ensure_ascii=False),
            structured=structured,
        )

    def _wait_tool_result(
        self,
        *,
        tool_call_id: str,
        wait: AgentWaitRequest,
        resolution: AgentWaitResolution,
    ) -> ToolResult:
        structured = self._wait_structured(
            wait.delegation_ids,
            reason=resolution.reason,
            wait_id=wait.wait_id,
        )
        return ToolResult(
            tool_call_id=tool_call_id,
            tool_name="WaitAgents",
            content=json.dumps(structured, ensure_ascii=False),
            structured=structured,
            ended_at=utc_now(),
        )

    def _wait_structured(
        self,
        delegation_ids: tuple[str, ...],
        *,
        reason: str,
        wait_id: str | None,
    ) -> dict[str, object]:
        agents: list[dict[str, object]] = []
        for delegation_id in delegation_ids:
            relation = self._load_by_id(delegation_id)
            item: dict[str, object] = {"handle": self._handle(relation).model_dump(mode="json")}
            if relation.result is not None:
                detail = self.read_agent(relation.delegation_id)
                item["result"] = {
                    "terminal_status": relation.result.terminal_status,
                    "result_message_id": relation.result.result_message_id,
                    "resource_refs": list(relation.result.resource_refs),
                    "usage": relation.result.usage,
                    "final_text": detail.final_text[:8_000],
                }
            agents.append(item)
        return {
            "wait_id": wait_id,
            "reason": reason,
            "agents": agents,
        }

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

    def _mark_waiter_delivery(
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
        self._record_delivery(relation, target_run_id=target_run_id)

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
        for call in batch.calls:
            if call.status != "waiting_child" or call.agent_wait is None:
                calls.append(call)
                continue
            resolved_ids = tuple(
                delegation_id
                for delegation_id in call.agent_wait.delegation_ids
                if self._is_terminal(self._load_by_id(delegation_id))
            )
            resolution = AgentWaitResolution(
                resolved_delegation_ids=resolved_ids,
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
                            content="Parent Run terminated while waiting for child Agents.",
                            structured={"interrupted": True},
                            ended_at=utc_now(),
                        ),
                    }
                )
            )
            changed = True
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

    def _resolve_parent_catalog(
        self,
        parent_run: RunRecord,
        agent_id: str,
    ) -> AgentCatalogEntry:
        if parent_run.agent_catalog is None:
            raise ValueError("parent Run has no frozen Agent catalog")
        return parent_run.agent_catalog.resolve(agent_id)

    def _require_root_quota(
        self,
        root_session_id: str,
        *,
        entry: AgentCatalogEntry,
    ) -> None:
        active_relations = [
            relation
            for relation in self.store.list_delegations(root_session_id)
            if not self._is_terminal(relation)
        ]
        active = len(active_relations)
        if active >= self.max_active_children_per_root:
            raise SessionBusyError("root active child quota is exhausted")
        requests_write = any(
            tool.effect == "state_changing" for tool in entry.effective_tools
        )
        if requests_write and any(
            relation.constraints.allow_write for relation in active_relations
        ):
            raise SessionBusyError("root write-capable child quota is exhausted")

    def _effective_child_budget(
        self,
        *,
        root_session_id: str,
        parent_run: RunRecord,
        requested: RunBudgetPolicy,
    ) -> RunBudgetPolicy:
        raw = parent_run.metadata.get("run_budget")
        if raw is None:
            return requested
        if not isinstance(raw, dict):
            raise ValueError("parent Run has invalid budget metadata")
        root_budget = RunBudgetPolicy.model_validate(raw)
        input_tokens = int(parent_run.usage.get("input_tokens", 0))
        output_tokens = int(parent_run.usage.get("output_tokens", 0))
        model_rounds = self._model_round_count(parent_run)
        for relation in self.store.list_delegations(root_session_id):
            if (
                relation.parent_run_id != parent_run.run_id
                or relation.child_session_id is None
            ):
                continue
            for child_run in self.store.load_runs(relation.child_session_id):
                input_tokens += int(child_run.usage.get("input_tokens", 0))
                output_tokens += int(child_run.usage.get("output_tokens", 0))
                model_rounds += self._model_round_count(child_run)

        elapsed_seconds = max(0.0, (utc_now() - parent_run.started_at).total_seconds())

        def remaining_int(limit: int | None, used: int, name: str) -> int | None:
            if limit is None:
                return None
            remaining = limit - used
            if remaining < 1:
                raise SessionBusyError(f"root {name} budget is exhausted")
            return remaining

        remaining_elapsed: float | None = None
        if root_budget.max_elapsed_seconds is not None:
            remaining_elapsed = root_budget.max_elapsed_seconds - elapsed_seconds
            if remaining_elapsed <= 0:
                raise SessionBusyError("root elapsed time budget is exhausted")

        remaining = RunBudgetPolicy(
            max_model_rounds=remaining_int(
                root_budget.max_model_rounds,
                model_rounds,
                "model round",
            ),
            max_input_tokens=remaining_int(
                root_budget.max_input_tokens,
                input_tokens,
                "input token",
            ),
            max_output_tokens=remaining_int(
                root_budget.max_output_tokens,
                output_tokens,
                "output token",
            ),
            max_elapsed_seconds=remaining_elapsed,
        )
        return requested.constrained_by(remaining)

    def _model_round_count(self, run: RunRecord) -> int:
        cursor = 0
        count = 0
        while True:
            events, next_cursor = self.store.load_events_page(
                run.session_id,
                cursor=cursor,
                limit=100,
                run_id=run.run_id,
            )
            count += sum(event.event_type == "model_request_started" for event in events)
            if next_cursor is None:
                return count
            cursor = next_cursor

    def _handle(self, relation: DelegationRecord) -> AgentHandleView:
        return AgentHandleView(
            delegation_id=relation.delegation_id,
            child_session_id=relation.child_session_id,
            child_run_id=relation.current_child_run_id,
            agent_id=relation.agent_id,
            agent_name=relation.definition.name,
            nickname=relation.nickname,
            status=self._status(relation),
            execution_mode=relation.execution_mode,
        )

    def _status(self, relation: DelegationRecord) -> str:
        if relation.spawn_state == "failed":
            return "failed"
        if relation.child_session_id is None or relation.current_child_run_id is None:
            return relation.spawn_state
        try:
            return self.store.load_run(
                relation.child_session_id, relation.current_child_run_id
            ).status
        except FileNotFoundError:
            return "recoverable"

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
        for relation in self._all_relations():
            if relation.delegation_id == delegation_id:
                return relation
        raise FileNotFoundError(delegation_id)

    def _all_relations(self) -> list[DelegationRecord]:
        relations: list[DelegationRecord] = []
        roots = {
            session.root_session_id or session.session_id
            for session in self.store.list_sessions()
            if session.session_kind == "primary"
        }
        for root_id in sorted(roots):
            relations.extend(self.store.list_delegations(root_id))
        return relations

    def _completion_event(self, delegation_id: str) -> asyncio.Event:
        return self._completion_events.setdefault(delegation_id, asyncio.Event())

    def _wait_finished(self, task: asyncio.Task[None], wait_id: str) -> None:
        self._watchers.discard(task)
        self._armed_wait_ids.discard(wait_id)


__all__ = ["AgentService", "RunScheduler"]
