from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from ouro.sdk.plugins.run_drivers import RunDriverContext


@dataclass
class PluginInvocation:
    owner_id: str
    writable: bool
    session_id: str | None = None
    run_id: str | None = None
    workspace: str | None = None
    workspace_only: bool = False
    active: bool = True
    native_run: RunDriverContext | None = None

    def require_active(self) -> None:
        if not self.active:
            raise RuntimeError("plugin invocation has finished")

    def close(self) -> None:
        self.active = False


current_invocation: ContextVar[PluginInvocation | None] = ContextVar(
    "ouro_plugin_invocation", default=None
)


@contextmanager
def plugin_invocation(
    owner_id: str,
    *,
    writable: bool,
    session_id: str | None = None,
    run_id: str | None = None,
    workspace: str | None = None,
    workspace_only: bool = False,
    native_run: RunDriverContext | None = None,
    inherit_run_ports: bool = True,
) -> Iterator[PluginInvocation]:
    parent = current_invocation.get()
    # A Host-accepted Driver starts a new Run scope; a child may outlive the
    # callback which accepted it. Ordinary nested calls inherit their caller.
    if parent is not None and native_run is None:
        parent.require_active()
        writable = writable and parent.writable
        session_id = parent.session_id
        run_id = parent.run_id
        workspace = parent.workspace
        workspace_only = parent.workspace_only
        native_run = parent.native_run if inherit_run_ports else None
    invocation = PluginInvocation(
        owner_id, writable, session_id, run_id, workspace, workspace_only, native_run=native_run
    )
    token = current_invocation.set(invocation)
    try:
        yield invocation
    finally:
        invocation.close()
        current_invocation.reset(token)
