"""Capabilities and resource lifetimes required by Run execution."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, AbstractContextManager
from typing import Protocol

from ouro.core.compaction import CompactionStrategyRegistry
from ouro.core.context_providers import RequestProviders
from ouro.core.continuations import RunContinuationRegistry
from ouro.core.observers import Observation, ObserverRegistry
from ouro.core.prompt_policies import PromptPolicyRegistry
from ouro.core.run_drivers import RunDriverBinding, RunDriverContext, RunDriverRegistration
from ouro.core.session_controller_registry import SessionControllerRegistry
from ouro.core.session_controls import SessionControlState
from ouro.core.state import RunContinuationControllerRef
from ouro.core.tool_policies import ToolPolicyRegistry
from ouro.core.turn_policies import TurnPolicyRegistry
from ouro.skills import SkillMetadata
from ouro.tools import ToolRegistry


class ContributionLease[T](Protocol):
    @property
    def value(self) -> T: ...

    def close(self) -> None: ...


class RuntimeCapabilities(Protocol):
    """One decision's capabilities, retained until its native operation settles."""

    @property
    def catalog_id(self) -> str: ...

    @property
    def tools(self) -> ToolRegistry: ...

    @property
    def observers(self) -> ObserverRegistry: ...

    @property
    def request_providers(self) -> RequestProviders: ...

    @property
    def compaction(self) -> CompactionStrategyRegistry: ...

    @property
    def continuations(self) -> RunContinuationRegistry: ...

    @property
    def prompt_policies(self) -> PromptPolicyRegistry: ...

    @property
    def tool_policies(self) -> ToolPolicyRegistry: ...

    @property
    def turn_policies(self) -> TurnPolicyRegistry: ...

    @property
    def skills(self) -> tuple[SkillMetadata, ...]: ...

    @property
    def session_controllers(self) -> SessionControllerRegistry: ...

    def generation_metadata(self) -> list[dict[str, object]]: ...

    def execution_contracts(self) -> dict[str, str]: ...

    def continuation_controllers(
        self, controls: SessionControlState
    ) -> list[RunContinuationControllerRef]: ...

    def bind_observers(
        self, observation: Observation, *, active_controller_ids: Callable[[], set[str]]
    ) -> ContributionLease[ObserverRegistry]: ...

    def close(self) -> None: ...


class RuntimeExtensions(Protocol):
    """Current environment access; installation and activation remain adapter-owned."""

    async def synchronize(self) -> None: ...

    def snapshot(self) -> RuntimeCapabilities: ...

    def select_driver(self, driver_id: str) -> RunDriverBinding: ...

    def bind_driver(
        self, expected: RunDriverBinding
    ) -> AbstractAsyncContextManager[ContributionLease[RunDriverRegistration]]: ...

    def driver_scope(self, context: RunDriverContext) -> AbstractContextManager[object]: ...

    async def replace_base_skills(self, skills: tuple[SkillMetadata, ...]) -> None: ...

    def bind_observers(
        self, observation: Observation, *, active_controller_ids: Callable[[], set[str]]
    ) -> ContributionLease[ObserverRegistry]: ...
