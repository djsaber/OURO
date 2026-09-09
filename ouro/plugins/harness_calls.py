from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import replace
from typing import Any, Literal

from ouro.core.callbacks import invoke_callback, invoke_owned_callback
from ouro.core.cancellation import AbortSignal
from ouro.core.harness_calls import HarnessCallSnapshot, HarnessInvocation, HarnessStateChange
from ouro.core.session_extensions import SessionExtensionStateRecord
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
    owned: bool = False,
) -> tuple[Any, HarnessStateChange | None]:
    """Give one callback a state view and close it before returning its proposal."""

    view = (
        PluginStateView(snapshot, owner_id, writable=writable, signal=signal)
        if snapshot is not None
        else None
    )
    try:
        bound_call = replace(call, session_state=view)
        args, kwargs = callback_arguments(handler, bound_call, context)
        if owned:
            raw = await invoke_owned_callback(
                handler, *args, on_cancel=view.close if view is not None else None, **kwargs
            )
        else:
            raw = await invoke_callback(handler, *args, **kwargs)
        return raw, view.finish() if view is not None else None
    finally:
        if view is not None:
            view.close()


def callback_arguments(
    handler: Callable[..., Any], call: Any, context: object | None
) -> tuple[tuple[object, ...], dict[str, object]]:
    if context is None:
        return (call,), {}
    try:
        parameters = tuple(inspect.signature(handler).parameters.values())
    except (TypeError, ValueError):
        return (call,), {}
    if any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD or parameter.name == "context"
        for parameter in parameters
    ):
        return (call,), {"context": context}
    positional = [
        parameter
        for parameter in parameters
        if parameter.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    return ((call, context), {}) if len(positional) >= 2 else ((call,), {})
