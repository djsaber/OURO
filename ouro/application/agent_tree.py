from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from typing import Final, Protocol

from ouro.application.agent_models import (
    AgentInteractionSummary,
    AgentInteractionView,
    AgentTreeEdge,
    AgentTreeNode,
    AgentTreeNotification,
    AgentTreeSnapshot,
)
from ouro.application.dto import Diagnostic
from ouro.application.supervisor_models import RuntimeActivitySnapshot, RuntimeNotification
from ouro.core.agents import DelegationRecord
from ouro.core.errors import SessionNotFoundError
from ouro.core.run_budget import RunBudgetPolicy
from ouro.core.state import RunRecord
from ouro.core.tool_batches import ToolBatchCall, ToolCallBatchRecord
from ouro.sessions import SessionStore

_CLOSED: Final = object()
_RELATION_EVENT_TYPES = frozenset(
    {
        "agent_spawned",
        "agent_result_ready",
        "agent_result_delivered",
        "agent_closed",
    }
)
_TERMINAL_STATUSES = frozenset({"completed", "failed", "aborted", "paused", "recoverable"})
_MAX_INTERACTION_ARGUMENT_PREVIEW = 240


class RuntimeSubscriptionPort(Protocol):
    session_id: str
    initial_activity: RuntimeActivitySnapshot | None

    async def get(self) -> RuntimeNotification: ...

    def close(self) -> None: ...


class AgentTreeRuntime(Protocol):
    def activity(self, handle_or_session: str) -> RuntimeActivitySnapshot: ...

    def subscribe(
        self,
        session_id: str,
        *,
        after_sequence: int = 0,
        max_buffer: int = 256,
        include_activity: bool = False,
    ) -> RuntimeSubscriptionPort: ...


class AgentTreeProjector:
    """Rebuild the bounded root Agent tree only from canonical records."""

    def __init__(self, *, store: SessionStore, runtime: AgentTreeRuntime) -> None:
        self.store = store
        self.runtime = runtime

    def snapshot(self, root_session_id: str) -> AgentTreeSnapshot:
        root = self.store.load_session_record(root_session_id)
        if root.session_kind != "primary" or root.root_session_id != root_session_id:
            raise ValueError("Agent tree root must be a primary root Session")

        root_run = self._selected_root_run(root_session_id)
        root_budget = self._run_budget(root_run)
        try:
            root_activity = self.runtime.activity(root_session_id)
        except (FileNotFoundError, KeyError, SessionNotFoundError):
            root_activity = None
        relations = self.store.list_delegations(root_session_id)
        nodes: list[AgentTreeNode] = []
        edges: list[AgentTreeEdge] = []
        active_count = 0
        waiting_count = 0
        terminal_count = 0
        pending_interaction_count = 0
        revision = self.store.latest_event_sequence(root_session_id)
        root_usage = dict(root_run.usage) if root_run is not None else {}

        for relation in relations:
            revision += relation.revision + 1
            if relation.child_session_id is None:
                continue
            try:
                revision += self.store.latest_event_sequence(relation.child_session_id)
            except FileNotFoundError:
                pass
            status = delegation_status(self.store, relation)
            if status in _TERMINAL_STATUSES:
                terminal_count += 1
            elif status.startswith("waiting"):
                waiting_count += 1
            else:
                active_count += 1
            interactions = self._interaction_summaries(relation)
            pending_interaction_count += len(interactions)
            usage = self._delegation_usage(relation)
            if root_run is not None and relation.parent_run_id == root_run.run_id:
                for key, value in usage.items():
                    root_usage[key] = root_usage.get(key, 0) + value
            try:
                activity = self.runtime.activity(relation.child_session_id)
            except (FileNotFoundError, KeyError, SessionNotFoundError):
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
                    lifecycle_binding=relation.lifecycle_binding,
                    control_state=relation.control_state,
                    depth=relation.depth,
                    usage=usage,
                    budget=relation.budget,
                    pending_interactions=interactions,
                    result_ready=relation.result is not None,
                    delivery_status=(
                        relation.result.delivery.status if relation.result is not None else None
                    ),
                    activity=activity,
                )
            )
            edges.append(
                AgentTreeEdge(
                    delegation_id=relation.delegation_id,
                    parent_session_id=relation.parent_session_id,
                    child_session_id=relation.child_session_id,
                )
            )

        return AgentTreeSnapshot(
            root_session_id=root_session_id,
            revision=revision,
            nodes=tuple(nodes),
            edges=tuple(edges),
            root_usage=root_usage,
            root_budget=root_budget,
            root_activity=root_activity,
            waiting_on_delegation_ids=self._waiting_delegation_ids(root_run),
            active_count=active_count,
            waiting_count=waiting_count,
            terminal_count=terminal_count,
            pending_interaction_count=pending_interaction_count,
        )

    def interactions(self, root_session_id: str) -> tuple[AgentInteractionView, ...]:
        interactions: list[AgentInteractionView] = []
        for relation in self.store.list_delegations(root_session_id):
            batch = self._current_child_batch(relation)
            if batch is None:
                continue
            for call in batch.calls:
                summary = self._interaction_summary(call, batch)
                if summary is None:
                    continue
                assert relation.child_session_id is not None
                assert relation.current_child_run_id is not None
                interactions.append(
                    AgentInteractionView(
                        kind=summary.kind,
                        delegation_id=relation.delegation_id,
                        agent_id=relation.agent_id,
                        agent_name=relation.definition.name,
                        task_description=relation.task_description,
                        child_session_id=relation.child_session_id,
                        child_run_id=relation.current_child_run_id,
                        tool_call_id=summary.tool_call_id,
                        tool_name=summary.tool_name,
                        request_id=summary.request_id,
                        requested_at=summary.requested_at.isoformat(),
                        arguments_preview=_arguments_preview(
                            call.invocation.arguments
                            if call.invocation is not None
                            else call.request.arguments
                        ),
                        input_request=(
                            call.input_request.data if call.input_request is not None else None
                        ),
                    )
                )
        return tuple(interactions)

    def live_session_ids(self, root_session_id: str) -> tuple[str, ...]:
        return tuple(
            relation.child_session_id
            for relation in self.store.list_delegations(root_session_id)
            if relation.child_session_id is not None and relation.control_state == "open"
        )

    def latest_sequence(self, session_id: str) -> int:
        return self.store.latest_event_sequence(session_id)

    def _selected_root_run(self, root_session_id: str) -> RunRecord | None:
        root = self.store.load_session_record(root_session_id)
        run_id = root.active_run_id or root.last_run_id
        return self.store.load_run(root_session_id, run_id) if run_id is not None else None

    @staticmethod
    def _run_budget(run: RunRecord | None) -> RunBudgetPolicy | None:
        if run is None:
            return None
        raw = run.metadata.get("run_budget")
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise ValueError("root Run has invalid budget metadata")
        return RunBudgetPolicy.model_validate(raw)

    @staticmethod
    def _waiting_delegation_ids(run: RunRecord | None) -> tuple[str, ...]:
        if run is None or run.active_tool_batch is None:
            return ()
        return tuple(
            dict.fromkeys(
                call.agent_wait.delegation_id
                for call in run.active_tool_batch.calls
                if call.status == "waiting_child" and call.agent_wait is not None
            )
        )

    def _delegation_usage(self, relation: DelegationRecord) -> dict[str, int | float]:
        if relation.child_session_id is None:
            return {}
        usage: dict[str, int | float] = {}
        try:
            runs = self.store.load_runs(relation.child_session_id)
        except FileNotFoundError:
            return usage
        for run in runs:
            for key, value in run.usage.items():
                usage[key] = usage.get(key, 0) + value
        return usage

    def _interaction_summaries(
        self,
        relation: DelegationRecord,
    ) -> tuple[AgentInteractionSummary, ...]:
        batch = self._current_child_batch(relation)
        if batch is None:
            return ()
        return tuple(
            summary
            for call in batch.calls
            if (summary := self._interaction_summary(call, batch)) is not None
        )

    def _current_child_batch(
        self,
        relation: DelegationRecord,
    ) -> ToolCallBatchRecord | None:
        if relation.child_session_id is None or relation.current_child_run_id is None:
            return None
        try:
            run = self.store.load_run(
                relation.child_session_id,
                relation.current_child_run_id,
            )
        except FileNotFoundError:
            return None
        return run.active_tool_batch

    @staticmethod
    def _interaction_summary(
        call: ToolBatchCall,
        batch: ToolCallBatchRecord,
    ) -> AgentInteractionSummary | None:
        if call.status == "awaiting_approval" and call.approval is not None:
            return AgentInteractionSummary(
                kind="approval",
                tool_call_id=call.request.id,
                tool_name=call.request.name,
                request_id=call.approval.approval_id,
                requested_at=call.approval.requested_at,
            )
        if call.status == "waiting_user" and call.input_request is not None:
            return AgentInteractionSummary(
                kind="user_input",
                tool_call_id=call.request.id,
                tool_name=call.request.name,
                request_id=f"{batch.batch_id}/{call.request.id}",
                requested_at=batch.updated_at,
            )
        return None


def _arguments_preview(arguments: dict[str, object]) -> str:
    preview = json.dumps(arguments, ensure_ascii=False)
    if len(preview) > _MAX_INTERACTION_ARGUMENT_PREVIEW:
        return preview[: _MAX_INTERACTION_ARGUMENT_PREVIEW - 1] + "…"
    return preview


class AgentTreeSubscription(AsyncIterator[AgentTreeNotification]):
    """Compose existing per-Session streams into a bounded root tree stream."""

    def __init__(
        self,
        *,
        root_session_id: str,
        projector: AgentTreeProjector,
        runtime: AgentTreeRuntime,
        max_buffer: int = 64,
        runtime_buffer: int = 64,
        on_close: Callable[[AgentTreeSubscription], None] | None = None,
    ) -> None:
        if max_buffer <= 0 or runtime_buffer <= 0:
            raise ValueError("Agent tree subscription buffers must be positive")
        self.root_session_id = root_session_id
        self.initial_snapshot = projector.snapshot(root_session_id)
        self._projector = projector
        self._runtime = runtime
        self._runtime_buffer = runtime_buffer
        self._on_close = on_close
        self._queue: asyncio.Queue[AgentTreeNotification | object] = asyncio.Queue(max_buffer)
        self._subscriptions: dict[str, RuntimeSubscriptionPort] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._closed = False
        self._open_runtime_subscription(root_session_id)
        self._sync_child_subscriptions()

    def __aiter__(self) -> AgentTreeSubscription:
        return self

    async def __anext__(self) -> AgentTreeNotification:
        item = await self._queue.get()
        if item is _CLOSED:
            raise StopAsyncIteration
        assert isinstance(item, AgentTreeNotification)
        return item

    async def get(self) -> AgentTreeNotification:
        return await self.__anext__()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for subscription in tuple(self._subscriptions.values()):
            subscription.close()
        for task in tuple(self._tasks.values()):
            task.cancel()
        self._subscriptions.clear()
        self._tasks.clear()
        self._offer_closed()
        if self._on_close is not None:
            self._on_close(self)

    def _open_runtime_subscription(self, session_id: str) -> None:
        if session_id in self._subscriptions or self._closed:
            return
        subscription = self._runtime.subscribe(
            session_id,
            after_sequence=self._projector.latest_sequence(session_id),
            max_buffer=self._runtime_buffer,
            include_activity=True,
        )
        self._subscriptions[session_id] = subscription
        self._tasks[session_id] = asyncio.create_task(
            self._consume_runtime(session_id, subscription),
            name=f"ouro-agent-tree-{self.root_session_id}-{session_id}",
        )

    def _sync_child_subscriptions(self) -> None:
        expected = set(self._projector.live_session_ids(self.root_session_id))
        current_children = set(self._subscriptions).difference({self.root_session_id})
        for session_id in sorted(expected.difference(current_children)):
            self._open_runtime_subscription(session_id)
        for session_id in sorted(current_children.difference(expected)):
            subscription = self._subscriptions.pop(session_id)
            subscription.close()
            task = self._tasks.pop(session_id)
            if task is not asyncio.current_task():
                task.cancel()

    async def _consume_runtime(
        self,
        session_id: str,
        subscription: RuntimeSubscriptionPort,
    ) -> None:
        try:
            while not self._closed:
                notification = await subscription.get()
                if notification.kind == "diagnostic":
                    self._offer_lagged(session_id)
                    continue
                if notification.kind == "event":
                    if (
                        session_id != self.root_session_id
                        or notification.event is None
                        or notification.event.event_type not in _RELATION_EVENT_TYPES
                    ):
                        continue
                self._sync_child_subscriptions()
                self._offer(
                    AgentTreeNotification(
                        kind="snapshot",
                        snapshot=self._projector.snapshot(self.root_session_id),
                    )
                )
                if session_id not in self._subscriptions:
                    return
        except (StopAsyncIteration, asyncio.CancelledError):
            return
        except Exception as exc:  # noqa: BLE001 - isolate each child subscription.
            self._offer_diagnostic(
                code="agent_tree_subscription_failed",
                session_id=session_id,
                message=f"Agent tree source failed ({type(exc).__name__}); reload the snapshot",
            )

    def _offer(self, notification: AgentTreeNotification) -> None:
        if self._closed:
            return
        try:
            self._queue.put_nowait(notification)
        except asyncio.QueueFull:
            self._clear_queue()
            self._offer_lagged(self.root_session_id)

    def _offer_lagged(self, session_id: str) -> None:
        self._offer_diagnostic(
            code="agent_tree_subscription_lagged",
            session_id=session_id,
            message="Agent tree subscriber buffer overflowed; reload the canonical snapshot",
        )

    def _offer_diagnostic(self, *, code: str, session_id: str, message: str) -> None:
        if self._closed:
            return
        self._clear_queue()
        diagnostic = Diagnostic(
            code=code,
            severity="warning",
            component="application.agents",
            message=message,
            session_id=session_id,
            recoverable=True,
            suggested_action="Call agent_tree_snapshot and continue from its revision.",
        )
        self._queue.put_nowait(AgentTreeNotification(kind="diagnostic", diagnostic=diagnostic))

    def _clear_queue(self) -> None:
        with suppress(asyncio.QueueEmpty):
            while True:
                self._queue.get_nowait()

    def _offer_closed(self) -> None:
        try:
            self._queue.put_nowait(_CLOSED)
        except asyncio.QueueFull:
            self._clear_queue()
            self._queue.put_nowait(_CLOSED)


def delegation_status(store: SessionStore, relation: DelegationRecord) -> str:
    if relation.spawn_state == "failed":
        return "failed"
    if relation.child_session_id is None or relation.current_child_run_id is None:
        return relation.spawn_state
    try:
        return store.load_run(
            relation.child_session_id,
            relation.current_child_run_id,
        ).status
    except FileNotFoundError:
        return "recoverable"


__all__ = [
    "AgentTreeProjector",
    "AgentTreeRuntime",
    "AgentTreeSubscription",
    "RuntimeSubscriptionPort",
    "delegation_status",
]
