"""SDK tool calls and invocation-local Session state over the worker protocol."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import asdict
from typing import Any, Literal

from . import v5 as sdk


class WorkerSessionState:
    def __init__(self, raw: Mapping[str, Any]) -> None:
        self._raw = raw
        self._value = deepcopy(raw["value"])
        self._active = True
        self._owner_task = asyncio.current_task()
        self.changed = False
        self.change: dict[str, Any] | None = None

    def _require_active(self) -> None:
        if not self._active or (self._owner_task is not None and self._owner_task.cancelling()):
            raise RuntimeError("plugin invocation has finished")

    @property
    def writable(self) -> bool:
        self._require_active()
        return self._raw["writable"]

    def read(self) -> sdk.PluginSessionState | None:
        self._require_active()
        value = deepcopy(self._value)
        return (
            sdk.PluginSessionState(value["state_schema_version"], value["payload"])
            if value
            else None
        )

    def controls(self) -> tuple[sdk.PluginSessionControl, ...]:
        self._require_active()
        return tuple(sdk.PluginSessionControl(**item) for item in deepcopy(self._raw["controls"]))

    def run_controls(self) -> tuple[sdk.PluginRunControl, ...]:
        self._require_active()
        return tuple(sdk.PluginRunControl(**item) for item in deepcopy(self._raw["run_controls"]))

    def usage_tokens(self) -> dict[str, int] | None:
        self._require_active()
        return deepcopy(self._raw["usage_tokens"])

    def _require_writable(self) -> None:
        if not self.writable:
            raise PermissionError("read-only Session state cannot be changed")

    def replace(
        self,
        *,
        state_schema_version: int,
        payload: sdk.JsonValue,
        branch_policy: Literal["copy", "reset", "reject"] = "reset",
        projection_semantic_tags: tuple[str, ...] = (),
    ) -> None:
        self._require_writable()
        self.change = {
            "state_schema_version": state_schema_version,
            "payload": deepcopy(payload),
            "branch_policy": branch_policy,
            "projection_semantic_tags": list(projection_semantic_tags),
        }
        self._value = self.change
        self.changed = True

    def delete(self) -> None:
        self._require_writable()
        self._value = self.change = None
        self.changed = True

    def close(self) -> None:
        self._active = False
        self._owner_task = None


async def invoke_worker_tool(
    tool: sdk.ToolContribution, params: Mapping[str, Any], *, resume: bool = False
) -> dict[str, Any]:
    """Invoke a one-argument SDK handler; Host validates and commits its result."""
    state = WorkerSessionState(params["session_state"]) if "session_state" in params else None
    raw = dict(params["call"])
    try:
        handler: Any
        if resume:
            resolution = raw["resolution"]
            raw["resolution"] = (
                sdk.AnswerResolution(**resolution)
                if resolution["kind"] == "answer"
                else sdk.UnattendedResolution(**resolution)
            )
            call: sdk.ToolCall | sdk.ToolResumeCall = sdk.ToolResumeCall(**raw, session_state=state)
            handler = tool.resume_handler
            if handler is None:
                raise ValueError("tool has no resume handler")
        else:
            call = sdk.ToolCall(**raw, session_state=state)
            handler = tool.handler
        output = await _invoke(handler, call)
        if isinstance(output, str):
            result: dict[str, Any] = {"content": output}
        elif isinstance(output, sdk.ToolInputRequest):
            result = {"input_request": output.data}
        elif isinstance(output, sdk.ToolOutput):
            result = {
                "content": output.content
                if isinstance(output.content, str)
                else [asdict(part) for part in output.content],
                "structured": dict(output.structured),
                "resource_refs": list(output.resource_refs),
                "is_error": output.is_error,
                "session_control_transitions": [
                    asdict(item) for item in output.session_control_transitions
                ],
            }
        else:
            raise TypeError("worker tool handler must return text, ToolOutput or ToolInputRequest")
        if state is not None and state.changed:
            result["session_state"] = deepcopy(state.change)
        return result
    finally:
        if state is not None:
            state.close()


async def _invoke(handler: Any, call: sdk.ToolCall | sdk.ToolResumeCall) -> object:
    if inspect.iscoroutinefunction(handler):
        return await handler(call)
    task = asyncio.create_task(asyncio.to_thread(handler, call))
    try:
        result = await asyncio.shield(task)
    except asyncio.CancelledError as cancellation:
        while not task.done():
            try:
                await asyncio.wait((task,))
            except asyncio.CancelledError:
                continue
        try:
            result = task.result()
        except Exception as exc:
            raise cancellation from exc
        if inspect.iscoroutine(result):
            result.close()
        raise
    return await result if inspect.isawaitable(result) else result
