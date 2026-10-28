"""Generation availability gates used by catalog bindings."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncGenerator, Iterable
from contextlib import asynccontextmanager
from typing import Any, Protocol

from ouro.core.callbacks import CallbackCancelled
from ouro.plugins.contributions.models import PreparedContributionSet
from ouro.plugins.models import PluginGenerationRecord


class PluginGenerationUnavailableError(CallbackCancelled):
    """Raised when a callback targets a generation whose invocation gate is closed."""

    def __init__(self, reason: str, *, result: object = None) -> None:
        super().__init__(result, reason=reason)


class GenerationGate:
    """Process-local admission gate shared by every callback in one generation."""

    def __init__(self, generation_id: str, *, initially_open: bool = True) -> None:
        self.generation_id = generation_id
        self._open = initially_open
        self._in_flight = 0
        self._invocations: dict[asyncio.Timeout, asyncio.Task[Any]] = {}
        self._lock = threading.Lock()
        self._drained = asyncio.Event()
        self._drained.set()

    @property
    def is_open(self) -> bool:
        with self._lock:
            return self._open

    @property
    def in_flight(self) -> int:
        with self._lock:
            return self._in_flight

    def close(self) -> None:
        with self._lock:
            self._open = False
            if self._in_flight == 0:
                self._drained.set()

    def open(self) -> None:
        with self._lock:
            if self._open:
                return
            self._open = True

    async def wait_drained(self) -> None:
        await self._drained.wait()

    def cancel_inflight(self) -> None:
        current = asyncio.current_task()
        with self._lock:
            invocations = tuple(self._invocations.items())
        for cancellation, task in invocations:
            if task is not current and not cancellation.expired():
                cancellation.reschedule(asyncio.get_running_loop().time())

    @asynccontextmanager
    async def invocation(self) -> AsyncGenerator[None]:
        task = asyncio.current_task()
        assert task is not None
        cancellation = asyncio.timeout(None)
        with self._lock:
            if not self._open:
                raise PluginGenerationUnavailableError(
                    f"plugin generation is unavailable: {self.generation_id}"
                )
            self._in_flight += 1
            self._invocations[cancellation] = task
            self._drained.clear()
        try:
            try:
                # The timeout scope restores its own cancellation request. A
                # plugin revocation must not cancel the surrounding Host task.
                async with cancellation:
                    yield
            except TimeoutError:
                if not cancellation.expired():
                    raise
            except CallbackCancelled as exc:
                if not cancellation.expired():
                    raise
                raise PluginGenerationUnavailableError(
                    f"plugin generation is unavailable: {self.generation_id}", result=exc.result
                ) from None
            if cancellation.expired():
                raise PluginGenerationUnavailableError(
                    f"plugin generation is unavailable: {self.generation_id}"
                )
        finally:
            with self._lock:
                self._in_flight -= 1
                del self._invocations[cancellation]
                if self._in_flight == 0:
                    self._drained.set()


class GenerationLease(Protocol):
    @property
    def record(self) -> PluginGenerationRecord: ...

    def close(self) -> None: ...


class CatalogGeneration(Protocol):
    record: PluginGenerationRecord
    contributions: PreparedContributionSet
    gate: GenerationGate

    def acquire(self) -> GenerationLease: ...


class CatalogToolGroup(CatalogGeneration, Protocol):
    group_id: str


def acquire_generations(generations: Iterable[CatalogGeneration]) -> tuple[GenerationLease, ...]:
    """Acquire a selected set atomically, rolling back partial acquisition."""
    leases: list[GenerationLease] = []
    try:
        for generation in generations:
            leases.append(generation.acquire())
    except BaseException:
        for lease in reversed(leases):
            lease.close()
        raise
    return tuple(leases)
