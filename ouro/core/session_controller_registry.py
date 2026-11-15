from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Literal

from pydantic import Field, JsonValue

from ouro.core.base import OuroModel
from ouro.core.callbacks import invoke_bounded_callback
from ouro.core.extensions import PluginSource
from ouro.core.harness_calls import HarnessCallSnapshot, HarnessStateChange
from ouro.core.redaction import redact_text
from ouro.core.session_controls import SessionControlClaim, SessionControlState

_CONTROLLER_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")


class SessionControllerCall(OuroModel):
    harness: HarnessCallSnapshot | None = None
    session_id: str
    session_kind: Literal["primary", "subagent"]
    activation_id: str
    controller_id: str
    action: Literal["activate", "deactivate"]
    payload: JsonValue = None
    reason: str | None = None
    workspace: str | None = None
    replacement_controller_id: str | None = None


class SessionControllerOutput(OuroModel):
    harness_change: HarnessStateChange | None = None
    output: str = ""
    data: dict[str, object] = Field(default_factory=dict)
    usage_scope: str | None = None


SessionControllerHandler = Callable[
    [SessionControllerCall],
    SessionControllerOutput | Awaitable[SessionControllerOutput],
]


class SessionControllerError(RuntimeError):
    pass


@dataclass(frozen=True)
class SessionController:
    controller_id: str
    description: str
    claims: tuple[SessionControlClaim, ...]
    activate_handler: SessionControllerHandler
    deactivate_handler: SessionControllerHandler
    timeout_seconds: float = 5.0
    plugin_id: str = ""
    source: PluginSource = "third_party"
    plugin_class: Literal["builtin", "user"] = "user"
    release_id: str | None = None
    package_digest: str | None = None
    owner: str | None = None
    generation_id: str | None = None
    contract_hash: str | None = None
    execution_contract_hash: str | None = None

    @property
    def canonical_id(self) -> str:
        return f"{self.plugin_id}/session_controllers/{self.controller_id}"

    async def activate(
        self,
        *,
        session_id: str,
        session_kind: Literal["primary", "subagent"],
        activation_id: str,
        payload: JsonValue = None,
        workspace: str | None = None,
        harness: HarnessCallSnapshot | None = None,
    ) -> SessionControllerOutput:
        return await self._invoke(
            self.activate_handler,
            SessionControllerCall(
                harness=harness,
                session_id=session_id,
                session_kind=session_kind,
                activation_id=activation_id,
                controller_id=self.canonical_id,
                action="activate",
                payload=payload,
                workspace=workspace,
            ),
        )

    async def deactivate(
        self,
        *,
        session_id: str,
        session_kind: Literal["primary", "subagent"],
        activation_id: str,
        payload: JsonValue = None,
        reason: str | None = None,
        workspace: str | None = None,
        harness: HarnessCallSnapshot | None = None,
        replacement_controller_id: str | None = None,
    ) -> SessionControllerOutput:
        return await self._invoke(
            self.deactivate_handler,
            SessionControllerCall(
                harness=harness,
                session_id=session_id,
                session_kind=session_kind,
                activation_id=activation_id,
                controller_id=self.canonical_id,
                action="deactivate",
                replacement_controller_id=replacement_controller_id,
                payload=payload,
                reason=reason,
                workspace=workspace,
            ),
        )

    async def _invoke(
        self,
        handler: SessionControllerHandler,
        call: SessionControllerCall,
    ) -> SessionControllerOutput:
        try:
            result = await invoke_bounded_callback(
                handler,
                call,
                timeout_seconds=self.timeout_seconds,
            )
        except TimeoutError as exc:
            raise SessionControllerError(
                f"session controller timed out after {self.timeout_seconds:g} seconds: "
                f"{self.canonical_id}"
            ) from exc
        except Exception as exc:
            raise SessionControllerError(
                f"session controller failed ({type(exc).__name__}): {redact_text(str(exc))}"
            ) from exc
        if not isinstance(result, SessionControllerOutput):
            raise SessionControllerError(
                f"session controller returned invalid output: {self.canonical_id}"
            )
        return result


class SessionControllerRegistry:
    """Immutable-catalog registry for declared Session controller contracts."""

    def __init__(self) -> None:
        self._controllers: dict[str, SessionController] = {}
        self._frozen = False

    def freeze(self) -> None:
        self._frozen = True

    def _require_mutable(self) -> None:
        if self._frozen:
            raise RuntimeError("session controller registry structure is frozen")

    def replace_owner(
        self,
        owner: str,
        controllers: tuple[SessionController, ...],
    ) -> None:
        self._require_mutable()
        updated = {
            identity: controller
            for identity, controller in self._controllers.items()
            if controller.owner != owner
        }
        for controller in controllers:
            prepared = replace(controller, owner=owner)
            self._validate(prepared)
            if prepared.canonical_id in updated:
                conflict = updated[prepared.canonical_id].owner or "builtin"
                raise ValueError(
                    "session controller already registered: "
                    f"{prepared.canonical_id} (owner: {conflict})"
                )
            updated[prepared.canonical_id] = prepared
        self._controllers = updated

    def unregister_owner(self, owner: str) -> None:
        self.replace_owner(owner, ())

    def get(self, canonical_id: str) -> SessionController:
        try:
            return self._controllers[canonical_id]
        except KeyError as exc:
            raise KeyError(f"unknown session controller: {canonical_id}") from exc

    def list(self) -> list[SessionController]:
        return [self._controllers[key] for key in sorted(self._controllers)]

    def unavailable_controls(self, controls: SessionControlState) -> tuple[str, ...]:
        """Resolve active contracts against this catalog, preserving activation order."""
        return tuple(
            activation.controller_id
            for activation in controls.activations
            if (controller := self._controllers.get(activation.controller_id)) is None
            or controller.execution_contract_hash != activation.execution_contract_hash
        )

    def snapshot(self) -> dict[str, SessionController]:
        return dict(self._controllers)

    def restore(self, snapshot: dict[str, SessionController]) -> None:
        self._require_mutable()
        self._controllers = dict(snapshot)

    @staticmethod
    def _validate(controller: SessionController) -> None:
        if not controller.plugin_id:
            raise ValueError("session controller requires plugin_id")
        if _CONTROLLER_ID.fullmatch(controller.controller_id) is None:
            raise ValueError("session controller id must be a lowercase stable identifier")
        if controller.timeout_seconds <= 0:
            raise ValueError("session controller timeout must be positive")
        domains = [claim.domain for claim in controller.claims]
        if not domains:
            raise ValueError("session controller requires at least one control claim")
        if len(domains) != len(set(domains)):
            raise ValueError("session controller cannot claim the same domain twice")
