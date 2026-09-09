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
from ouro.core.session_controls import SessionControlClaim
from ouro.plugins.models import PluginClass

_CONTROLLER_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")


class PluginSessionControllerCall(OuroModel):
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


class PluginSessionControllerOutput(OuroModel):
    harness_change: HarnessStateChange | None = None
    output: str = ""
    data: dict[str, object] = Field(default_factory=dict)
    usage_scope: str | None = None


PluginSessionControllerHandler = Callable[
    [PluginSessionControllerCall],
    PluginSessionControllerOutput | Awaitable[PluginSessionControllerOutput],
]


class PluginSessionControllerError(RuntimeError):
    pass


@dataclass(frozen=True)
class PluginSessionController:
    controller_id: str
    description: str
    claims: tuple[SessionControlClaim, ...]
    activate_handler: PluginSessionControllerHandler
    deactivate_handler: PluginSessionControllerHandler
    timeout_seconds: float = 5.0
    plugin_id: str = ""
    source: PluginSource = "third_party"
    plugin_class: PluginClass = "user"
    release_id: str | None = None
    package_digest: str | None = None
    builtin_contract_hash: str | None = None
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
    ) -> PluginSessionControllerOutput:
        return await self._invoke(
            self.activate_handler,
            PluginSessionControllerCall(
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
    ) -> PluginSessionControllerOutput:
        return await self._invoke(
            self.deactivate_handler,
            PluginSessionControllerCall(
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
        handler: PluginSessionControllerHandler,
        call: PluginSessionControllerCall,
    ) -> PluginSessionControllerOutput:
        try:
            result = await invoke_bounded_callback(
                handler,
                call,
                timeout_seconds=self.timeout_seconds,
            )
        except TimeoutError as exc:
            raise PluginSessionControllerError(
                f"session controller timed out after {self.timeout_seconds:g} seconds: "
                f"{self.canonical_id}"
            ) from exc
        except Exception as exc:
            raise PluginSessionControllerError(
                f"session controller failed ({type(exc).__name__}): {redact_text(str(exc))}"
            ) from exc
        if not isinstance(result, PluginSessionControllerOutput):
            raise PluginSessionControllerError(
                f"session controller returned invalid output: {self.canonical_id}"
            )
        return result


class PluginSessionControllerRegistry:
    """Immutable-catalog registry for declared Session controller contracts."""

    def __init__(self) -> None:
        self._controllers: dict[str, PluginSessionController] = {}
        self._frozen = False

    def freeze(self) -> None:
        self._frozen = True

    def _require_mutable(self) -> None:
        if self._frozen:
            raise RuntimeError("session controller registry structure is frozen")

    def replace_owner(
        self,
        owner: str,
        controllers: tuple[PluginSessionController, ...],
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

    def get(self, canonical_id: str) -> PluginSessionController:
        try:
            return self._controllers[canonical_id]
        except KeyError as exc:
            raise KeyError(f"unknown session controller: {canonical_id}") from exc

    def list(self) -> list[PluginSessionController]:
        return [self._controllers[key] for key in sorted(self._controllers)]

    def snapshot(self) -> dict[str, PluginSessionController]:
        return dict(self._controllers)

    def restore(self, snapshot: dict[str, PluginSessionController]) -> None:
        self._require_mutable()
        self._controllers = dict(snapshot)

    @staticmethod
    def _validate(controller: PluginSessionController) -> None:
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


def bind_plugin_session_controller(
    controller: PluginSessionController,
    *,
    plugin_id: str,
    source: PluginSource,
    plugin_class: PluginClass,
    release_id: str,
    package_digest: str | None,
    builtin_contract_hash: str | None,
    generation_id: str,
    contract_hash: str | None = None,
    execution_contract_hash: str | None = None,
    maximum_timeout_seconds: float,
) -> PluginSessionController:
    return replace(
        controller,
        plugin_id=plugin_id,
        source=source,
        plugin_class=plugin_class,
        release_id=release_id,
        package_digest=package_digest,
        builtin_contract_hash=builtin_contract_hash,
        generation_id=generation_id,
        contract_hash=contract_hash,
        execution_contract_hash=execution_contract_hash,
        timeout_seconds=min(controller.timeout_seconds, maximum_timeout_seconds),
    )


__all__ = [
    "PluginSessionController",
    "PluginSessionControllerCall",
    "PluginSessionControllerError",
    "PluginSessionControllerOutput",
    "PluginSessionControllerRegistry",
    "bind_plugin_session_controller",
]
