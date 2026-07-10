from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import Field

from ouro.core.base import OuroModel

HookName = Literal[
    "before_run",
    "after_run",
    "before_context_build",
    "after_context_build",
    "before_model_call",
    "after_model_response",
    "before_tool_call",
    "after_tool_call",
    "on_approval_requested",
    "on_event",
    "on_error",
]

PluginSource = Literal["official", "third_party", "ai_generated"]


class HookContext(OuroModel):
    hook_name: HookName
    session_id: str | None = None
    run_id: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class HookResult(OuroModel):
    added_context: list[str] = Field(default_factory=list)
    diagnostics: list[str] = Field(default_factory=list)


class HookFailure(OuroModel):
    plugin_id: str
    hook_name: HookName
    session_id: str | None = None
    run_id: str | None = None
    error_type: str
    message: str


HookHandler = Callable[[HookContext], HookResult | Awaitable[HookResult] | None]


@dataclass(frozen=True)
class HookRegistration:
    plugin_id: str
    hook_name: HookName
    handler: HookHandler
    source: PluginSource = "third_party"
    priority: int = 100
    enabled: bool = True


@dataclass
class HookDispatchResult:
    results: list[HookResult] = field(default_factory=list)
    failures: list[HookFailure] = field(default_factory=list)


_SOURCE_ORDER: dict[PluginSource, int] = {
    "official": 0,
    "third_party": 1,
    "ai_generated": 2,
}


class HookRegistry:
    def __init__(self) -> None:
        self._registrations: list[HookRegistration] = []
        self._disabled_for_run: set[tuple[str | None, str, HookName]] = set()

    def register(self, registration: HookRegistration) -> None:
        self._registrations.append(registration)

    def registered(self, hook_name: HookName | None = None) -> list[HookRegistration]:
        registrations = self._registrations
        if hook_name is not None:
            registrations = [item for item in registrations if item.hook_name == hook_name]
        return sorted(
            registrations,
            key=lambda item: (_SOURCE_ORDER[item.source], item.priority, item.plugin_id),
        )

    def finish_run(self, run_id: str) -> None:
        """Forget per-run hook isolation state after a run reaches a terminal state."""

        self._disabled_for_run = {key for key in self._disabled_for_run if key[0] != run_id}

    async def dispatch(self, context: HookContext) -> HookDispatchResult:
        dispatch_result = HookDispatchResult()
        for registration in self.registered(context.hook_name):
            if not registration.enabled:
                continue
            disabled_key = (context.run_id, registration.plugin_id, registration.hook_name)
            if disabled_key in self._disabled_for_run:
                continue
            try:
                raw_result = registration.handler(context)
                if inspect.isawaitable(raw_result):
                    raw_result = await raw_result
                if raw_result is not None:
                    dispatch_result.results.append(raw_result)
            except Exception as exc:  # noqa: BLE001 - hook isolation boundary
                failure = HookFailure(
                    plugin_id=registration.plugin_id,
                    hook_name=registration.hook_name,
                    session_id=context.session_id,
                    run_id=context.run_id,
                    error_type=type(exc).__name__,
                    message=str(exc),
                )
                dispatch_result.failures.append(failure)
                if registration.source != "official":
                    self._disabled_for_run.add(disabled_key)
        return dispatch_result
