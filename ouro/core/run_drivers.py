from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal, Protocol, Self

from pydantic import ConfigDict, Field, JsonValue, model_validator

from ouro.core.base import OuroModel
from ouro.core.messages import MessageRecord
from ouro.core.plugin_contracts import PluginImplementation
from ouro.core.tools import ToolCallRequest, ToolResult

DEFAULT_DRIVER_ID = "ouro.default"
DEFAULT_DRIVER_CONTRACT = "ouro.default/1"
MAX_DRIVER_CHECKPOINT_BYTES = 64 * 1024


class RunDriverBinding(OuroModel):
    model_config = ConfigDict(frozen=True)

    driver_id: str = DEFAULT_DRIVER_ID
    contract_hash: str = DEFAULT_DRIVER_CONTRACT
    implementation: PluginImplementation | None = None
    dependencies: tuple[PluginImplementation, ...] = ()

    @property
    def is_default(self) -> bool:
        return self.driver_id == DEFAULT_DRIVER_ID

    @model_validator(mode="after")
    def validate_contract(self) -> Self:
        if self.is_default:
            if self.contract_hash != DEFAULT_DRIVER_CONTRACT or any(
                (self.implementation, self.dependencies)
            ):
                raise ValueError("unsupported default Run driver contract")
        elif (
            self.implementation is None
            or not self.contract_hash
            or not self.driver_id.startswith(f"{self.implementation.plugin_id}/run_drivers/")
        ):
            raise ValueError("plugin Run driver requires an exact implementation contract")
        elif len({item.plugin_id for item in self.implementations}) != len(self.implementations):
            raise ValueError("Run driver implementation identities must be unique")
        return self

    @property
    def implementations(self) -> tuple[PluginImplementation, ...]:
        return (self.implementation, *self.dependencies) if self.implementation else ()


class DriverCheckpoint(OuroModel):
    batch_id: str = Field(min_length=1)
    state: Literal["pending", "ready", "consumed"] = "pending"
    payload: JsonValue

    @model_validator(mode="after")
    def validate_size(self) -> Self:
        encoded = json.dumps(self.payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(encoded) > MAX_DRIVER_CHECKPOINT_BYTES:
            raise ValueError("Driver checkpoint exceeds 64 KiB; store large output as a resource")
        return self


@dataclass(frozen=True)
class DriverModelResponse:
    response_id: str
    text: str
    reasoning: str
    tool_calls: tuple[ToolCallRequest, ...]


@dataclass(frozen=True)
class DriverToolResult:
    suspended: bool
    results: tuple[ToolResult, ...] = ()


@dataclass(frozen=True)
class DriverResume:
    checkpoint: JsonValue
    results: tuple[ToolResult, ...]


@dataclass(frozen=True)
class DriverFinished:
    text: str
    response_id: str | None = None


@dataclass(frozen=True)
class DriverSuspended:
    pass


@dataclass(frozen=True)
class DriverInputDelivery:
    sequence: int
    messages: list[MessageRecord] = field(default_factory=list)


DriverInputConsumer = Callable[..., Awaitable[DriverInputDelivery]]


class RunDriverContext(Protocol):
    @property
    def workspace(self) -> str | None: ...

    @property
    def workspace_only(self) -> bool: ...

    @property
    def session_id(self) -> str: ...

    @property
    def run_id(self) -> str: ...

    @property
    def is_aborted(self) -> bool: ...

    async def consume_inputs(self) -> tuple[MessageRecord, ...]: ...

    async def request_model(
        self, *, prompt: str = "", tools: tuple[str, ...] | None = None
    ) -> DriverModelResponse: ...

    async def execute_model_tools(
        self, response_id: str, *, checkpoint: JsonValue
    ) -> DriverToolResult: ...

    async def execute_tools(
        self, calls: tuple[ToolCallRequest, ...], *, checkpoint: JsonValue
    ) -> DriverToolResult: ...


RunDriverHandler = Callable[
    [RunDriverContext, DriverResume | None], Awaitable[DriverFinished | DriverSuspended]
]
