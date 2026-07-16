from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from pydantic import Field

from ouro.core.base import OuroModel
from ouro.core.callbacks import invoke_bounded_callback
from ouro.core.redaction import redact_text
from ouro.core.state import RunOutcome

HookName = Literal[
    "after_run",
    "after_context_build",
    "before_tool_call",
    "after_tool_call",
    "on_event",
]

PluginSource = Literal["official", "third_party", "ai_generated"]


class HookContext(OuroModel):
    hook_name: HookName
    session_id: str | None = None
    run_id: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    run_outcome: RunOutcome | None = None


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
    timeout_seconds: float | None = None
    hook_id: str = ""
    owner: str | None = None
    generation_id: str | None = None


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
    def __init__(
        self,
        *,
        semantic_timeout_seconds: float = 10.0,
        observational_timeout_seconds: float = 1.0,
    ) -> None:
        if semantic_timeout_seconds <= 0 or observational_timeout_seconds <= 0:
            raise ValueError("hook timeouts must be positive")
        self._registrations: list[HookRegistration] = []
        self._disabled_for_run: set[tuple[str | None, str, HookName, str]] = set()
        self._semantic_timeout_seconds = semantic_timeout_seconds
        self._observational_timeout_seconds = observational_timeout_seconds

    def register(self, registration: HookRegistration) -> None:
        self.register_many((registration,))

    def register_many(self, registrations: tuple[HookRegistration, ...]) -> None:
        for registration in registrations:
            if registration.timeout_seconds is not None and registration.timeout_seconds <= 0:
                raise ValueError("hook timeout must be positive")
        self._registrations.extend(registrations)

    def replace_owner(
        self,
        owner: str,
        registrations: tuple[HookRegistration, ...],
    ) -> None:
        for registration in registrations:
            if registration.timeout_seconds is not None and registration.timeout_seconds <= 0:
                raise ValueError("hook timeout must be positive")
        retained = [item for item in self._registrations if item.owner != owner]
        identities = [(item.plugin_id, item.hook_name, item.hook_id) for item in registrations]
        if len(identities) != len(set(identities)):
            raise ValueError("replacement contains duplicate hook identities")
        self._registrations = [
            *retained,
            *(replace(registration, owner=owner) for registration in registrations),
        ]

    def unregister_owner(self, owner: str) -> None:
        self.replace_owner(owner, ())

    def snapshot(
        self,
    ) -> tuple[list[HookRegistration], set[tuple[str | None, str, HookName, str]]]:
        return list(self._registrations), set(self._disabled_for_run)

    def restore(
        self,
        snapshot: tuple[
            list[HookRegistration],
            set[tuple[str | None, str, HookName, str]],
        ],
    ) -> None:
        self._registrations = list(snapshot[0])
        self._disabled_for_run = set(snapshot[1])

    def registered(self, hook_name: HookName | None = None) -> list[HookRegistration]:
        registrations = self._registrations
        if hook_name is not None:
            registrations = [item for item in registrations if item.hook_name == hook_name]
        return sorted(
            registrations,
            key=lambda item: (
                _SOURCE_ORDER[item.source],
                item.priority,
                item.plugin_id,
                item.hook_id,
                item.owner or "",
            ),
        )

    def finish_run(self, run_id: str) -> None:
        """Forget per-run hook isolation state after a run reaches a terminal state."""

        self._disabled_for_run = {key for key in self._disabled_for_run if key[0] != run_id}

    async def dispatch(self, context: HookContext) -> HookDispatchResult:
        dispatch_result = HookDispatchResult()
        for registration in self.registered(context.hook_name):
            if not registration.enabled:
                continue
            identity = registration.hook_id or f"handler:{id(registration.handler)}"
            disabled_key = (
                context.run_id,
                registration.plugin_id,
                registration.hook_name,
                f"{registration.owner or ''}:{identity}",
            )
            if disabled_key in self._disabled_for_run:
                continue
            try:
                timeout = registration.timeout_seconds or self._default_timeout(
                    registration.hook_name
                )
                raw_result = await invoke_bounded_callback(
                    registration.handler,
                    context,
                    timeout_seconds=timeout,
                )
                if raw_result is not None:
                    if not isinstance(raw_result, HookResult):
                        raise TypeError("hook handler must return HookResult or None")
                    dispatch_result.results.append(raw_result)
            except TimeoutError:
                failure = HookFailure(
                    plugin_id=registration.plugin_id,
                    hook_name=registration.hook_name,
                    session_id=context.session_id,
                    run_id=context.run_id,
                    error_type="TimeoutError",
                    message=f"hook timed out after {timeout:g} seconds",
                )
                dispatch_result.failures.append(failure)
                if registration.source != "official":
                    self._disabled_for_run.add(disabled_key)
            except Exception as exc:  # noqa: BLE001 - hook isolation boundary
                failure = HookFailure(
                    plugin_id=registration.plugin_id,
                    hook_name=registration.hook_name,
                    session_id=context.session_id,
                    run_id=context.run_id,
                    error_type=type(exc).__name__,
                    message=redact_text(str(exc)),
                )
                dispatch_result.failures.append(failure)
                if registration.source != "official":
                    self._disabled_for_run.add(disabled_key)
        return dispatch_result

    def _default_timeout(self, hook_name: HookName) -> float:
        if hook_name == "on_event":
            return self._observational_timeout_seconds
        return self._semantic_timeout_seconds
