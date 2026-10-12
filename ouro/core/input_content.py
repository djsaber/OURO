"""User-authored text, uploaded copies and original-file references in one submission."""

from __future__ import annotations

from pathlib import PurePosixPath, PureWindowsPath
from typing import Annotated, Literal, Self

from pydantic import Field, field_validator, model_validator

from ouro.core.base import OuroModel


class InputElement(OuroModel):
    text_offset: int = Field(default=0, ge=0)
    # Preserve interleaved files and skills at the same plain-text offset.
    order: int = Field(default=0, ge=0)


class InputFileItem(InputElement):
    name: str = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not value.strip() or any(char in value for char in "/\\\r\n\t\x00"):
            raise ValueError("attachment name must be a single file name")
        return value


class InputAttachment(InputFileItem):
    kind: Literal["upload"] = "upload"
    resource_id: str = Field(min_length=1)

    @property
    def label(self) -> str:
        return self.name


class InputFileReference(InputFileItem):
    kind: Literal["reference"] = "reference"
    path: str = Field(min_length=1)
    workspace: str | None = None

    @field_validator("path", "workspace")
    @classmethod
    def validate_path(cls, value: str | None) -> str | None:
        if value is not None and (
            any(char in value for char in "\r\n\x00")
            or not (PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute())
        ):
            raise ValueError("file reference requires an absolute path")
        return value

    @property
    def label(self) -> str:
        return "@" + self.name


InputFile = Annotated[InputAttachment | InputFileReference, Field(discriminator="kind")]


class InputSkillReference(InputElement):
    name: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

    @property
    def label(self) -> str:
        return "@" + self.name


class InputContent(OuroModel):
    text: str = ""
    attachments: tuple[InputFile, ...] = ()
    skills: tuple[InputSkillReference, ...] = ()

    @model_validator(mode="after")
    def validate_positions(self) -> Self:
        offsets = [item.text_offset for item in self.attachments]
        if offsets != sorted(offsets) or any(offset > len(self.text) for offset in offsets):
            raise ValueError("attachment positions must be ordered within the input text")
        offsets = [item.text_offset for item in self.skills]
        if offsets != sorted(offsets) or any(offset > len(self.text) for offset in offsets):
            raise ValueError("skill positions must be ordered within the input text")
        return self

    @property
    def elements(self) -> tuple[InputFile | InputSkillReference, ...]:
        return tuple(
            sorted(
                (*self.attachments, *self.skills), key=lambda item: (item.text_offset, item.order)
            )
        )

    @classmethod
    def from_elements(
        cls, text: str, elements: tuple[InputFile | InputSkillReference, ...]
    ) -> InputContent:
        """Capture display order when files and skills share a text position."""
        if any(isinstance(item, InputSkillReference) for item in elements):
            elements = tuple(
                item.model_copy(update={"order": order}) for order, item in enumerate(elements)
            )
        return cls(
            text=text,
            attachments=tuple(
                item for item in elements if not isinstance(item, InputSkillReference)
            ),
            skills=tuple(item for item in elements if isinstance(item, InputSkillReference)),
        )

    @property
    def resource_refs(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                item.resource_id for item in self.attachments if isinstance(item, InputAttachment)
            )
        )

    @property
    def is_empty(self) -> bool:
        return not self.text.strip() and not self.attachments and not self.skills

    def followed_by(self, other: InputContent) -> InputContent:
        """Preserve both drafts when restoring an input into an occupied composer."""
        if self.is_empty:
            return other
        if other.is_empty:
            return self
        prefix = self.text + "\n"
        existing_skills = {item.name for item in self.skills}
        return InputContent(
            text=prefix + other.text,
            attachments=(
                *self.attachments,
                *(
                    item.model_copy(update={"text_offset": len(prefix) + item.text_offset})
                    for item in other.attachments
                ),
            ),
            skills=(
                *self.skills,
                *(
                    item.model_copy(update={"text_offset": len(prefix) + item.text_offset})
                    for item in other.skills
                    if item.name not in existing_skills
                ),
            ),
        )
