from __future__ import annotations

import os
from typing import Literal

from pydantic import Field

from ouro.core.base import OuroModel
from ouro.core.errors import CredentialError


class CredentialRef(OuroModel):
    type: Literal["env", "inline", "none"] = "none"
    name: str | None = None
    value: str | None = Field(default=None, repr=False)

    @classmethod
    def parse(cls, value: str | None) -> CredentialRef:
        if not value:
            return cls(type="none")
        if value.startswith("env:"):
            return cls(type="env", name=value.removeprefix("env:"))
        return cls(type="inline", value=value)

    def display(self) -> str:
        if self.type == "env":
            return f"env:{self.name}"
        if self.type == "inline":
            return "inline:***"
        return "none"


class CredentialResolver:
    def resolve(self, ref: CredentialRef) -> str | None:
        if ref.type == "none":
            return None
        if ref.type == "inline":
            return ref.value
        if ref.type == "env":
            if not ref.name:
                raise CredentialError("environment credential reference has no name")
            value = os.environ.get(ref.name)
            if not value:
                raise CredentialError(f"missing credential {ref.display()}")
            return value
        raise CredentialError(f"unsupported credential type {ref.type}")
