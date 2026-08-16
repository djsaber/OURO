from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from pydantic import Field

from ouro.core.base import OuroModel
from ouro.core.callbacks import invoke_bounded_callback
from ouro.core.errors import OuroError
from ouro.core.extensions import PluginSource
from ouro.core.redaction import redact_text
from ouro.core.session_controls import (
    AttributedSessionControlTransition,
    AttributedSessionInput,
    AttributedSessionInputBatch,
    SessionControlTransitionRequest,
    SessionInputBatchRequest,
    SessionInputRequest,
)

_COMMAND_NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")


class PluginCommandRequest(OuroModel):
    command_id: str
    argument: str = ""
    session_id: str | None = None
    workspace: str | None = None


class PluginCommandResult(OuroModel):
    command_id: str
    output: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    session_inputs: list[SessionInputRequest] = Field(default_factory=list)
    attributed_session_inputs: list[AttributedSessionInput] = Field(default_factory=list)
    session_input_batches: list[SessionInputBatchRequest] = Field(default_factory=list)
    attributed_session_input_batches: list[AttributedSessionInputBatch] = Field(
        default_factory=list
    )
    session_control_transitions: list[SessionControlTransitionRequest] = Field(default_factory=list)
    attributed_control_transitions: list[AttributedSessionControlTransition] = Field(
        default_factory=list
    )


class PluginCommandError(OuroError):
    pass


PluginCommandHandler = Callable[
    [PluginCommandRequest],
    str
    | Mapping[str, Any]
    | PluginCommandResult
    | Awaitable[str | Mapping[str, Any] | PluginCommandResult],
]


@dataclass(frozen=True)
class PluginCommandArgument:
    value: str
    description: str
    usage: str = ""


@dataclass(frozen=True)
class PluginCommand:
    name: str
    handler: PluginCommandHandler
    description: str = ""
    usage: str = ""
    argument_options: tuple[PluginCommandArgument, ...] = ()
    plugin_id: str = ""
    source: PluginSource = "third_party"
    timeout_seconds: float = 5.0
    owner: str | None = None
    generation_id: str | None = None
    execution_contract_hash: str | None = None
    allow_session_inputs: bool = False
    requires_session: bool = False

    @property
    def command_id(self) -> str:
        return f"{self.plugin_id}:{self.name}"


class PluginCommandRegistry:
    """Host-neutral, namespaced command registry with a typed execution boundary."""

    def __init__(self) -> None:
        self._commands: dict[str, PluginCommand] = {}
        self._frozen = False

    def freeze(self) -> None:
        self._frozen = True

    def _require_mutable(self) -> None:
        if self._frozen:
            raise RuntimeError("plugin command registry structure is frozen")

    def register(self, command: PluginCommand) -> None:
        self.register_many((command,))

    def register_many(self, commands: tuple[PluginCommand, ...]) -> None:
        self._require_mutable()
        updated = dict(self._commands)
        for command in commands:
            if not command.plugin_id:
                raise ValueError("plugin command requires plugin_id")
            if _COMMAND_NAME.fullmatch(command.name) is None:
                raise ValueError(
                    "plugin command name must use lowercase letters, numbers, '_' or '-'"
                )
            if command.timeout_seconds <= 0:
                raise ValueError("plugin command timeout must be positive")
            if command.command_id in updated:
                raise ValueError(f"plugin command already registered: {command.command_id}")
            updated[command.command_id] = command
        self._commands = updated

    def replace_owner(self, owner: str, commands: tuple[PluginCommand, ...]) -> None:
        self._require_mutable()
        updated = {
            identity: command
            for identity, command in self._commands.items()
            if command.owner != owner
        }
        for command in commands:
            prepared = replace(command, owner=owner)
            if not prepared.plugin_id:
                raise ValueError("plugin command requires plugin_id")
            if _COMMAND_NAME.fullmatch(prepared.name) is None:
                raise ValueError(
                    "plugin command name must use lowercase letters, numbers, '_' or '-'"
                )
            if prepared.timeout_seconds <= 0:
                raise ValueError("plugin command timeout must be positive")
            if prepared.command_id in updated:
                conflict = updated[prepared.command_id].owner or "builtin"
                raise ValueError(
                    f"plugin command already registered: {prepared.command_id} (owner: {conflict})"
                )
            updated[prepared.command_id] = prepared
        self._commands = updated

    def unregister_owner(self, owner: str) -> None:
        self.replace_owner(owner, ())

    def snapshot(self) -> dict[str, PluginCommand]:
        return dict(self._commands)

    def restore(self, snapshot: dict[str, PluginCommand]) -> None:
        self._require_mutable()
        self._commands = dict(snapshot)

    def get(self, command_id: str) -> PluginCommand:
        try:
            return self._commands[command_id]
        except KeyError as exc:
            raise KeyError(f"unknown plugin command: {command_id}") from exc

    def list(self) -> list[PluginCommand]:
        return [self._commands[name] for name in sorted(self._commands)]

    async def execute(
        self,
        command_id: str,
        argument: str = "",
        *,
        session_id: str | None = None,
        workspace: str | None = None,
    ) -> PluginCommandResult:
        command = self.get(command_id)
        if command.requires_session and session_id is None:
            raise PluginCommandError(f"plugin command requires a target Session: {command_id}")
        request = PluginCommandRequest(
            command_id=command_id,
            argument=argument,
            session_id=session_id,
            workspace=workspace,
        )
        try:
            raw = await invoke_bounded_callback(
                command.handler,
                request,
                timeout_seconds=command.timeout_seconds,
            )
        except TimeoutError as exc:
            raise PluginCommandError(
                f"plugin command timed out after {command.timeout_seconds:g} seconds: {command_id}"
            ) from exc
        except Exception as exc:
            raise PluginCommandError(
                f"plugin command failed ({type(exc).__name__}): {redact_text(str(exc))}"
            ) from exc
        if isinstance(raw, PluginCommandResult):
            result = (
                raw.model_copy(update={"command_id": command_id})
                if raw.command_id != command_id
                else raw
            )
        elif isinstance(raw, str):
            result = PluginCommandResult(command_id=command_id, output=raw)
        else:
            result = PluginCommandResult(command_id=command_id, data=dict(raw))
        if (
            result.session_inputs or result.session_input_batches
        ) and not command.allow_session_inputs:
            raise PluginCommandError(
                "plugin command requires sessions.write and background.run permissions: "
                f"{command_id}"
            )
        if result.session_inputs and result.session_input_batches:
            raise PluginCommandError(
                "plugin command cannot mix individual inputs with input batches"
            )
        if (
            not result.session_inputs
            and not result.session_input_batches
            and not result.session_control_transitions
        ):
            return result
        if command.generation_id is None or command.execution_contract_hash is None:
            raise PluginCommandError(
                f"plugin command has no active execution contract: {command_id}"
            )
        return result.model_copy(
            update={
                "attributed_session_inputs": [
                    AttributedSessionInput(
                        prompt=item.prompt,
                        mode=item.mode,
                        title=item.title,
                        role=item.role,
                        scaffold=item.scaffold,
                        semantic_tag=item.semantic_tag,
                        plugin_id=command.plugin_id,
                        contribution_id=command.name,
                        generation_id=command.generation_id,
                        execution_contract_hash=command.execution_contract_hash,
                    )
                    for item in result.session_inputs
                ],
                "attributed_session_input_batches": [
                    AttributedSessionInputBatch(
                        inputs=tuple(
                            AttributedSessionInput(
                                prompt=item.prompt,
                                mode=item.mode,
                                title=item.title,
                                role=item.role,
                                scaffold=item.scaffold,
                                semantic_tag=item.semantic_tag,
                                plugin_id=command.plugin_id,
                                contribution_id=command.name,
                                generation_id=command.generation_id,
                                execution_contract_hash=command.execution_contract_hash,
                            )
                            for item in batch.inputs
                        ),
                        coalesce_key=batch.coalesce_key,
                    )
                    for batch in result.session_input_batches
                ],
                "attributed_control_transitions": [
                    AttributedSessionControlTransition(
                        **item.model_dump(mode="python"),
                        plugin_id=command.plugin_id,
                        generation_id=command.generation_id,
                        execution_contract_hash=command.execution_contract_hash,
                    )
                    for item in result.session_control_transitions
                ],
            }
        )


def bind_plugin_command(
    command: PluginCommand,
    *,
    plugin_id: str,
    source: PluginSource,
    maximum_timeout_seconds: float,
) -> PluginCommand:
    return replace(
        command,
        plugin_id=plugin_id,
        source=source,
        timeout_seconds=min(command.timeout_seconds, maximum_timeout_seconds),
    )
