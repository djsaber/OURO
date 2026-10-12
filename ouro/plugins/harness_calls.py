from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from inspect import Parameter, iscoroutinefunction, signature
from typing import Any, Literal

from ouro.core.callbacks import invoke_owned_callback
from ouro.core.cancellation import AbortSignal
from ouro.core.harness_calls import HarnessCallSnapshot, HarnessInvocation, HarnessStateChange
from ouro.core.session_extensions import SessionExtensionStateRecord
from ouro.plugins.invocation import plugin_invocation
from ouro.sdk.plugins.state import (
    JsonValue,
    PluginRunControl,
    PluginSessionControl,
    PluginSessionState,
)


class PluginStateView:
    """Expose one framework-bound owner without exposing the Session repository."""

    def __init__(
        self,
        snapshot: HarnessCallSnapshot,
        owner_id: str,
        *,
        writable: bool,
        signal: AbortSignal | None = None,
    ) -> None:
        self._session_id = snapshot.session_id
        self._owner_id = owner_id
        self._writable = writable
        self._invocation = HarnessInvocation(snapshot, owner_id, writable=writable, signal=signal)

    @property
    def writable(self) -> bool:
        return self._writable

    def read(self) -> PluginSessionState | None:
        record = self._invocation.read()
        if record is None:
            return None
        return PluginSessionState(
            state_schema_version=record.state_schema_version, payload=record.payload
        )

    def controls(self) -> tuple[PluginSessionControl, ...]:
        return tuple(
            PluginSessionControl(
                controller_id=item.controller_local_id,
                activation_id=item.activation_id,
                data=item.data,
                usage_scope=item.usage_scope,
            )
            for item in self._invocation.controls()
        )

    def run_controls(self) -> tuple[PluginRunControl, ...]:
        return tuple(
            PluginRunControl(
                controller_id=item.controller_local_id,
                activation_id=item.activation_id,
                usage_scope=item.usage_scope,
            )
            for item in self._invocation.run_controls()
        )

    def usage_tokens(self) -> dict[str, int] | None:
        return self._invocation.usage_tokens()

    def replace(
        self,
        *,
        state_schema_version: int,
        payload: JsonValue,
        branch_policy: Literal["copy", "reset", "reject"] = "reset",
        projection_semantic_tags: tuple[str, ...] = (),
    ) -> None:
        self._invocation.replace(
            SessionExtensionStateRecord(
                session_id=self._session_id,
                owner_id=self._owner_id,
                state_schema_version=state_schema_version,
                payload=payload,
                branch_policy=branch_policy,
                projection_semantic_tags=projection_semantic_tags,
            )
        )

    def delete(self) -> None:
        self._invocation.delete()

    def finish(self) -> HarnessStateChange | None:
        return self._invocation.finish()

    def close(self) -> None:
        self._invocation.close()


async def invoke_plugin_callback(
    handler: Callable[..., Any],
    call: Any,
    *,
    snapshot: HarnessCallSnapshot | None,
    owner_id: str,
    writable: bool,
    signal: AbortSignal | None = None,
    context: object | None = None,
) -> tuple[Any, HarnessStateChange | None]:
    """Give one callback a state view and close it before returning its proposal."""

    view = (
        PluginStateView(snapshot, owner_id, writable=writable, signal=signal)
        if snapshot is not None
        else None
    )
    try:
        bound_call = replace(call, session_state=view)
        args = (bound_call,) if context is None else (bound_call, context)
        with plugin_invocation(
            owner_id,
            writable=writable,
            session_id=snapshot.session_id if snapshot is not None else None,
            run_id=getattr(call, "run_id", None),
            workspace=getattr(call, "workspace", None),
            workspace_only=getattr(call, "workspace_only", False),
        ) as invocation:

            def close() -> None:
                invocation.close()
                if view is not None:
                    view.close()

            raw = await invoke_owned_callback(handler, *args, on_cancel=close)
        return raw, view.finish() if view is not None else None
    finally:
        if view is not None:
            view.close()


def bind_context_callback(handler: Callable[..., Any]) -> Callable[[Any, Any], Any]:
    """Bind the SDK's optional context once, preserving async/sync ownership."""

    contract = signature(handler)
    parameters = tuple(contract.parameters.values())
    positional = [
        parameter
        for parameter in parameters
        if parameter.kind in (Parameter.POSITIONAL_ONLY, Parameter.POSITIONAL_OR_KEYWORD)
    ]
    if len(positional) >= 2 and positional[1].name == "context":
        contract.bind(None, None)
        return handler
    if any(
        parameter.kind == Parameter.VAR_KEYWORD
        or (parameter.name == "context" and parameter.kind != Parameter.POSITIONAL_ONLY)
        for parameter in parameters
    ):
        contract.bind(None, context=None)

        def bound(call, context):
            return handler(call, context=context)

    elif len(positional) >= 2:
        contract.bind(None, None)
        return handler
    else:
        contract.bind(None)

        def bound(call, context):
            return handler(call)

    if iscoroutinefunction(handler):

        async def async_bound(call, context):
            return await bound(call, context)

        return async_bound
    return bound
