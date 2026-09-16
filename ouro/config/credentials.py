from __future__ import annotations

import os
import re
from typing import Literal

from pydantic import ConfigDict, Field, model_validator

from ouro.core.base import OuroModel
from ouro.core.errors import CredentialError


class CredentialRef(OuroModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    type: Literal["env", "inline", "none", "managed"] = "none"
    name: str | None = None
    value: str | None = Field(default=None, repr=False)

    @model_validator(mode="after")
    def _validate_managed_reference(self) -> CredentialRef:
        if self.type == "managed":
            if self.name is None or re.fullmatch(r"cred_[0-9a-f]{32}", self.name) is None:
                raise ValueError("managed credential name must be a generated credential id")
            if self.value is not None:
                raise ValueError("managed credential reference cannot contain a secret value")
        return self

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
        if self.type == "managed":
            return f"managed:{self.name}"
        return "none"


class CredentialResolver:
    def resolve(self, ref: CredentialRef) -> str | None:
        if ref.type == "managed":
            raise CredentialError("managed credentials require the authentication service")
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
