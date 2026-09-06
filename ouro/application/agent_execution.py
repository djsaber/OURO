from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

from ouro.application.events import ApplicationEventPublisher
from ouro.application.supervisor_models import RunHandle, RuntimeStatusSnapshot
from ouro.core.agents import DelegationRecord
from ouro.core.callbacks import run_blocking
from ouro.core.errors import DelegationRevisionConflictError, PersistenceError, SessionBusyError
from ouro.core.redaction import redact_text
from ouro.core.state import RunRecord
from ouro.sessions import SessionStore

_LOGGER = logging.getLogger(__name__)


class RunObserver(Protocol):
    async def wait_run(self, session_id: str, run_id: str) -> RunRecord: ...

    async def wait(self, handle: RunHandle) -> RuntimeStatusSnapshot: ...

    def release_handle(self, handle: RunHandle) -> None: ...


class AgentExecutionTasks:
    """Own result settlement tasks by durable child Run identity.

    Retries are bounded and scoped to observed delegations. Pending delivery
    remains durable after exhaustion and can be retried by targeted recovery.
    No Session enumeration, per-token checkpoint reads, or extra polling loop.
    """

    def __init__(
        self,
        store: SessionStore,
        observer: RunObserver,
        events: ApplicationEventPublisher,
        settle: Callable[[DelegationRecord], Awaitable[object]],
    ) -> None:
        self.store = store
        self.observer = observer
        self.events = events
        self.settle = settle
        self._tasks: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._receipts: set[asyncio.Task[None]] = set()
        self._closed = False

    def watch(self, relation: DelegationRecord) -> None:
        if (
            self._closed
            or relation.child_session_id is None
            or relation.current_child_run_id is None
            or (relation.result is not None and relation.result.delivery.status != "pending")
        ):
            return
        key = relation.delegation_id, relation.current_child_run_id
        current = self._tasks.get(key)
        if current is not None and not current.done():
            return
        task = asyncio.create_task(self._observe(relation), name=f"ouro-agent-result-{key[1]}")
        self._tasks[key] = task
        task.add_done_callback(lambda done: self._finished(key, done))

    def own_receipt(self, handle: RunHandle) -> None:
        """Release private process receipts; result waits use durable identity."""
        if self._closed:
            return

        async def release() -> None:
            await self.observer.wait(handle)
            self.observer.release_handle(handle)

        task = asyncio.create_task(release(), name=f"ouro-agent-receipt-{handle.handle_id}")
        self._receipts.add(task)
        task.add_done_callback(self._receipt_finished)

    def _receipt_finished(self, task: asyncio.Task[None]) -> None:
        self._receipts.discard(task)
        if not task.cancelled() and task.exception() is not None:
            _LOGGER.error("Agent receipt cleanup failed: %s", type(task.exception()).__name__)

    async def close(self) -> None:
        self._closed = True
        tasks = (*self._tasks.values(), *self._receipts)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._receipts.clear()

    def _finished(self, key: tuple[str, str], task: asyncio.Task[None]) -> None:
        if self._tasks.get(key) is task:
            self._tasks.pop(key)
        if not task.cancelled() and task.exception() is not None:
            # Reporting itself can fail if storage is unavailable. Keep this
            # visible without logging prompts, tool payloads, or raw exceptions.
            _LOGGER.error(
                "Agent settlement reporting failed: %s (%s)", key, type(task.exception()).__name__
            )

    async def _observe(self, relation: DelegationRecord) -> None:
        async def settle() -> object:
            assert (
                relation.child_session_id is not None and relation.current_child_run_id is not None
            )
            await self.observer.wait_run(relation.child_session_id, relation.current_child_run_id)
            return await self.settle(relation)

        await self.retry(relation, settle)

    async def retry(
        self, relation: DelegationRecord, operation: Callable[[], Awaitable[object]]
    ) -> None:
        for attempt, delay in enumerate((0, 0.1, 0.5, 2.0)):
            if delay:
                await asyncio.sleep(delay)
            try:
                await operation()
                return
            except Exception as exc:
                retryable = isinstance(exc, (OSError, PersistenceError, SessionBusyError))
                error = f"{type(exc).__name__}: {redact_text(str(exc))}"[:4_000]
                try:
                    await run_blocking(self._retain_error, relation, error)
                except (OSError, PersistenceError):
                    # The failed dependency may be the same store used for
                    # diagnostics. Still spend only the bounded retry budget.
                    if retryable and attempt < 3:
                        continue
                    raise
                if retryable and attempt < 3:
                    continue
                await self.events.publish(
                    session_id=relation.root_session_id,
                    run_id=relation.parent_run_id,
                    event_type="diagnostic",
                    payload={
                        "code": "agent_result_delivery_incomplete",
                        "severity": "error",
                        "component": "application.agents",
                        "delegation_id": relation.delegation_id,
                        "child_run_id": relation.current_child_run_id,
                        "error": error,
                        "recoverable": True,
                        "suggested_action": "Reconcile this root Session to retry settlement.",
                    },
                )
                return

    def _retain_error(self, relation: DelegationRecord, error: str) -> None:
        for _ in range(3):
            current = self.store.load_delegation(relation.root_session_id, relation.delegation_id)
            result = current.result
            if (
                result is None
                or result.child_run_id != relation.current_child_run_id
                or result.delivery.status != "pending"
            ):
                return
            try:
                self.store.save_delegation(
                    current.model_copy(
                        update={
                            "result": result.model_copy(
                                update={
                                    "delivery": result.delivery.model_copy(update={"error": error})
                                }
                            )
                        }
                    ),
                    expected_revision=current.revision,
                )
                return
            except DelegationRevisionConflictError:
                continue
        raise DelegationRevisionConflictError("Could not retain pending result delivery error")
