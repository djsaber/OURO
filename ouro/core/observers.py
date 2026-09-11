from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from types import MappingProxyType
from typing import Literal

from ouro.core.base import OuroModel
from ouro.core.extensions import PluginSource
from ouro.core.harness_calls import HarnessCallSnapshot
from ouro.core.state import RunOrigin, RunStatus
from ouro.core.tools import ToolBatchBehavior, ToolEffect

ObservationName = Literal[
    "runtime_event",
    "context_prepared",
    "tool_call_requested",
    "tool_call_finished",
    "run_finished",
]

type FrozenJson = (
    None | bool | int | float | str | tuple[FrozenJson, ...] | Mapping[str, FrozenJson]
)


def freeze_json(value: object) -> FrozenJson:
    """Copy JSON-like data into recursively immutable containers.

    Observation construction must never make a completed business operation fail.
    Unsupported values are represented by a stable marker instead of invoking an
    arbitrary ``repr`` that could disclose plugin-owned data.
    """

    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): freeze_json(item) for key, item in value.items() if isinstance(key, str)}
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(freeze_json(item) for item in value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return freeze_json(model_dump(mode="json"))
        except Exception:  # noqa: BLE001 - observation normalization is fail-open.
            return "<unsupported>"
    return "<unsupported>"


def thaw_json(value: FrozenJson) -> object:
    if isinstance(value, Mapping):
        return {key: thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_json(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class RuntimeEventView:
    event_id: str
    session_id: str
    run_id: str | None
    turn_id: str | None
    event_type: str
    created_at: datetime
    sequence: int
    history_epoch: int = 0
    payload: Mapping[str, FrozenJson] = field(default_factory=lambda: MappingProxyType({}))
    metadata: Mapping[str, FrozenJson] = field(default_factory=lambda: MappingProxyType({}))


@dataclass(frozen=True, slots=True)
class RunOutcomeView:
    session_id: str
    run_id: str
    status: RunStatus
    origin: RunOrigin
    final_text: str = ""
    error: str | None = None
    usage: Mapping[str, int | float] = field(default_factory=lambda: MappingProxyType({}))


@dataclass(frozen=True, slots=True)
class RuntimeEventObservation:
    harness: HarnessCallSnapshot | None = field(default=None, kw_only=True)
    session_id: str
    run_id: str | None
    record: RuntimeEventView
    event: Literal["runtime_event"] = field(init=False, default="runtime_event")


@dataclass(frozen=True, slots=True)
class ContextPreparedObservation:
    harness: HarnessCallSnapshot | None = field(default=None, kw_only=True)
    session_id: str
    run_id: str
    message_count: int
    tool_count: int
    model_round: int
    context_metadata: Mapping[str, FrozenJson] = field(default_factory=lambda: MappingProxyType({}))
    event: Literal["context_prepared"] = field(init=False, default="context_prepared")


@dataclass(frozen=True, slots=True)
class ToolCallRequestedObservation:
    harness: HarnessCallSnapshot | None = field(default=None, kw_only=True)
    session_id: str
    run_id: str
    tool_call_id: str
    tool_name: str
    canonical_tool_id: str
    owner_id: str
    effect: ToolEffect
    batch_behavior: ToolBatchBehavior
    arguments: Mapping[str, FrozenJson] = field(default_factory=lambda: MappingProxyType({}))
    event: Literal["tool_call_requested"] = field(
        init=False,
        default="tool_call_requested",
    )


@dataclass(frozen=True, slots=True)
class ToolCallFinishedObservation:
    harness: HarnessCallSnapshot | None = field(default=None, kw_only=True)
    session_id: str
    run_id: str
    tool_call_id: str
    tool_name: str
    canonical_tool_id: str
    owner_id: str
    effect: ToolEffect
    batch_behavior: ToolBatchBehavior
    is_error: bool
    content: str
    structured: Mapping[str, FrozenJson] = field(default_factory=lambda: MappingProxyType({}))
    resource_refs: tuple[str, ...] = ()
    exit_code: int | None = None
    event: Literal["tool_call_finished"] = field(init=False, default="tool_call_finished")


@dataclass(frozen=True, slots=True)
class RunFinishedObservation:
    harness: HarnessCallSnapshot | None = field(default=None, kw_only=True)
    session_id: str
    run_id: str
    outcome: RunOutcomeView
    event: Literal["run_finished"] = field(init=False, default="run_finished")


type Observation = (
    RuntimeEventObservation
    | ContextPreparedObservation
    | ToolCallRequestedObservation
    | ToolCallFinishedObservation
    | RunFinishedObservation
)
ObserverHandler = Callable[[Observation], None | Awaitable[None]]


@dataclass(frozen=True, slots=True)
class ObserverRegistration:
    plugin_id: str
    observer_id: str
    event: ObservationName
    handler: ObserverHandler
    source: PluginSource = "third_party"
    priority: int = 100
    enabled: bool = True
    timeout_seconds: float = 1.0
    owner: str | None = None
    generation_id: str | None = None
    active_when_controller_id: str | None = None
    event_types: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.observer_id:
            raise ValueError("observer_id must not be empty")
        if self.timeout_seconds <= 0:
            raise ValueError("observer timeout must be positive")
        if self.event != "runtime_event" and self.event_types:
            raise ValueError("event_types is only valid for runtime_event observers")
        if len(self.event_types) != len(set(self.event_types)):
            raise ValueError("observer event_types must be unique")

    @property
    def delivery_key(self) -> tuple[str, str, str, ObservationName]:
        return (self.owner or "", self.plugin_id, self.observer_id, self.event)


class ObserverFailure(OuroModel):
    plugin_id: str
    observer_id: str
    observation: ObservationName
    session_id: str | None = None
    run_id: str | None = None
    error_type: str
    message: str


class ObserverRegistry:
    """Immutable-catalog registry for declarations, filtering and stable identity."""

    def __init__(self) -> None:
        self._registrations: list[ObserverRegistration] = []
        self._frozen = False
        self._active_controller_ids: Callable[[], set[str]] | None = None

    def freeze(self) -> None:
        self._frozen = True

    def _require_mutable(self) -> None:
        if self._frozen:
            raise RuntimeError("observer registry structure is frozen")

    def register(self, registration: ObserverRegistration) -> None:
        self.register_many((registration,))

    def register_many(self, registrations: tuple[ObserverRegistration, ...]) -> None:
        self._require_mutable()
        self._registrations.extend(registrations)

    def replace_owner(
        self,
        owner: str,
        registrations: tuple[ObserverRegistration, ...],
    ) -> None:
        self._require_mutable()
        identities = [(item.plugin_id, item.observer_id, item.event) for item in registrations]
        if len(identities) != len(set(identities)):
            raise ValueError("replacement contains duplicate observer identities")
        retained = [item for item in self._registrations if item.owner != owner]
        self._registrations = [
            *retained,
            *(replace(registration, owner=owner) for registration in registrations),
        ]

    def unregister_owner(self, owner: str) -> None:
        self.replace_owner(owner, ())

    def snapshot(self) -> list[ObserverRegistration]:
        return list(self._registrations)

    def restore(self, snapshot: list[ObserverRegistration]) -> None:
        self._require_mutable()
        self._registrations = list(snapshot)

    def registered(
        self,
        event: ObservationName | None = None,
        *,
        runtime_event_type: str | None = None,
    ) -> list[ObserverRegistration]:
        registrations = self._registrations
        if self._active_controller_ids is not None:
            active = self._active_controller_ids()
            registrations = [
                item
                for item in registrations
                if item.active_when_controller_id is None
                or item.active_when_controller_id in active
            ]
        if event is not None:
            registrations = [item for item in registrations if item.event == event]
        if event == "runtime_event" and runtime_event_type is not None:
            registrations = [
                item
                for item in registrations
                if not item.event_types or runtime_event_type in item.event_types
            ]
        return sorted(
            (item for item in registrations if item.enabled),
            key=lambda item: (
                item.priority,
                item.plugin_id,
                item.observer_id,
                item.owner or "",
            ),
        )

    def matching(self, observation: Observation) -> list[ObserverRegistration]:
        runtime_event_type = (
            observation.record.event_type
            if isinstance(observation, RuntimeEventObservation)
            else None
        )
        return self.registered(observation.event, runtime_event_type=runtime_event_type)

    def has_registration(
        self,
        event: ObservationName,
        *,
        runtime_event_type: str | None = None,
        active_controller_ids: set[str] | Callable[[], set[str]] | None = None,
    ) -> bool:
        """Check for one enabled match without sorting or building an Observation."""

        active: set[str] | None = (
            active_controller_ids if isinstance(active_controller_ids, set) else None
        )
        active_resolver: Callable[[], set[str]] | None
        if callable(active_controller_ids):
            active_resolver = active_controller_ids
        elif active_controller_ids is None:
            active_resolver = self._active_controller_ids
        else:
            active_resolver = None
        is_scoped = active is not None or active_resolver is not None
        for registration in self._registrations:
            if not registration.enabled or registration.event != event:
                continue
            if is_scoped and registration.active_when_controller_id is not None:
                if active is None:
                    assert active_resolver is not None
                    active = active_resolver()
                if registration.active_when_controller_id not in active:
                    continue
            if (
                event == "runtime_event"
                and runtime_event_type is not None
                and registration.event_types
                and runtime_event_type not in registration.event_types
            ):
                continue
            return True
        return False

    def scoped(self, active_controller_ids: Callable[[], set[str]]) -> ObserverRegistry:
        scoped = ObserverRegistry()
        scoped.restore(self.snapshot())
        scoped._active_controller_ids = active_controller_ids
        scoped.freeze()
        return scoped


__all__ = [
    "ContextPreparedObservation",
    "FrozenJson",
    "Observation",
    "ObservationName",
    "ObserverFailure",
    "ObserverHandler",
    "ObserverRegistration",
    "ObserverRegistry",
    "RunFinishedObservation",
    "RunOutcomeView",
    "RuntimeEventObservation",
    "RuntimeEventView",
    "ToolCallFinishedObservation",
    "ToolCallRequestedObservation",
    "freeze_json",
    "thaw_json",
]
