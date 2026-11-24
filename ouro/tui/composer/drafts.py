"""Durable input drafts owned exclusively by the TUI."""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from pydantic import Field, ValidationError, field_validator

from ouro.application.inputs.requests import UserMessageEdit
from ouro.core.base import VersionedOuroModel, new_id
from ouro.core.errors import ConfigError
from ouro.core.input_content import InputContent, InputFile, InputSkillReference
from ouro.local_io import read_text_atomic, write_model_atomic
from ouro.tui.input_presentation import ContextElement
from ouro.tui.view_models import TuiDraftConfiguration

MAX_SESSION_DRAFTS = 20


class TuiEditTarget(UserMessageEdit):
    number: int = Field(ge=1)
    summary: str

    def as_selection(self) -> UserMessageEdit:
        return UserMessageEdit(
            message_id=self.message_id,
            history_epoch=self.history_epoch,
            message_count=self.message_count,
        )


class TuiEditDraft(InputContent):
    target: TuiEditTarget


class TuiSessionDraft(InputContent):
    session_id: str
    edit: TuiEditDraft | None = None

    def with_edit(self, target: TuiEditTarget, content: InputContent) -> TuiSessionDraft:
        edit = TuiEditDraft(
            target=target, text=content.text, attachments=content.attachments, skills=content.skills
        )
        return self.model_copy(update={"edit": edit})

    @field_validator("session_id")
    @classmethod
    def validate_session_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("session_id must not be empty")
        return value


@dataclass(frozen=True, kw_only=True)
class MessageEditContext(ContextElement):
    target: TuiEditTarget
    previous_draft: TuiSessionDraft


class TuiPendingInput(InputContent):
    """A submission retained until its durable acceptance can be established."""

    input_id: str = Field(min_length=1)
    session_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
    origin_session_id: str | None = None
    edit: TuiEditTarget | None = None


class TuiInputDrafts(VersionedOuroModel):
    """Welcome draft plus an MRU window of Session-owned drafts."""

    schema_version: int = 4
    welcome: InputContent = Field(default_factory=InputContent)
    sessions: tuple[TuiSessionDraft, ...] = ()
    welcome_resource_id: str | None = Field(default=None, pattern=r"^ses_[0-9a-f]{32}$")
    welcome_workspace: str | None = None
    welcome_configuration: TuiDraftConfiguration | None = None
    pending: tuple[TuiPendingInput, ...] = ()

    @field_validator("sessions")
    @classmethod
    def validate_sessions(
        cls,
        value: tuple[TuiSessionDraft, ...],
    ) -> tuple[TuiSessionDraft, ...]:
        if len(value) > MAX_SESSION_DRAFTS:
            raise ValueError(f"sessions must contain at most {MAX_SESSION_DRAFTS} entries")
        session_ids = tuple(draft.session_id for draft in value)
        if len(set(session_ids)) != len(session_ids):
            raise ValueError("sessions must contain unique session IDs")
        if any(
            not (draft.text or draft.attachments or draft.skills or draft.edit) for draft in value
        ):
            raise ValueError("session drafts must not be empty")
        return value

    def draft_for(self, session_id: str | None) -> str:
        return self.content_for(session_id).text

    def resources_for(self, session_id: str | None) -> tuple[str, ...]:
        return self.content_for(session_id).resource_refs

    def content_for(self, session_id: str | None) -> InputContent:
        if session_id is None:
            return self.welcome
        draft = self._session_draft(session_id)
        if draft is None:
            return InputContent()
        content = draft.edit or draft
        return InputContent(
            text=content.text, attachments=content.attachments, skills=content.skills
        )

    def _session_draft(self, session_id: str | None) -> TuiSessionDraft | None:
        return next((draft for draft in self.sessions if draft.session_id == session_id), None)

    def context_for(self, session_id: str | None) -> MessageEditContext | None:
        draft = self._session_draft(session_id)
        if draft is None or draft.edit is None:
            return None
        target = draft.edit.target
        return MessageEditContext(
            label=f"Edit #{target.number}",
            preview=target.summary,
            target=target,
            previous_draft=draft.model_copy(update={"edit": None}),
        )

    def remember_composer(
        self, session_id: str, content: InputContent, context: ContextElement | None
    ) -> TuiInputDrafts:
        """Store the visible content and its destination as a single session draft."""
        if isinstance(context, MessageEditContext):
            draft = context.previous_draft.with_edit(context.target, content)
        else:
            draft = TuiSessionDraft(
                session_id=session_id,
                text=content.text,
                attachments=content.attachments,
                skills=content.skills,
            )
        return self._save_session(draft)

    def edit_message(
        self, session_id: str, target: TuiEditTarget, content: InputContent
    ) -> TuiInputDrafts:
        draft = self._session_draft(session_id) or TuiSessionDraft(session_id=session_id)
        return self._save_session(draft.with_edit(target, content))

    def finish_edit(self, session_id: str) -> TuiInputDrafts:
        draft = self._session_draft(session_id)
        return self._save_session(draft.model_copy(update={"edit": None})) if draft else self

    def remember(
        self,
        session_id: str | None,
        text: str,
        *,
        attachments: tuple[InputFile, ...] = (),
        skills: tuple[InputSkillReference, ...] = (),
    ) -> TuiInputDrafts:
        """Return state with one draft updated and empty drafts removed."""

        if session_id is None:
            content = InputContent(text=text, attachments=attachments, skills=skills)
            if content == self.welcome:
                return self
            return self.model_copy(update={"welcome": content})

        draft = self._session_draft(session_id) or TuiSessionDraft(session_id=session_id)
        current = draft.edit or draft
        if current.text == text and current.attachments == attachments and current.skills == skills:
            return self
        content = InputContent(text=text, attachments=attachments, skills=skills)
        if draft.edit is not None:
            return self._save_session(draft.with_edit(draft.edit.target, content))
        return self.remember_composer(session_id, content, None)

    def _save_session(self, draft: TuiSessionDraft) -> TuiInputDrafts:
        remaining = tuple(item for item in self.sessions if item.session_id != draft.session_id)
        sessions = (
            (draft, *remaining)[:MAX_SESSION_DRAFTS]
            if draft.text or draft.attachments or draft.skills or draft.edit
            else remaining
        )
        return self.model_copy(update={"sessions": sessions})

    def forget_sessions(self, session_ids: Iterable[str]) -> TuiInputDrafts:
        forgotten = frozenset(session_ids)
        if not forgotten:
            return self
        sessions = tuple(draft for draft in self.sessions if draft.session_id not in forgotten)
        if sessions == self.sessions:
            return self
        return self.model_copy(update={"sessions": sessions})

    def begin_submission(
        self,
        content: InputContent,
        *,
        session_id: str | None,
        workspace: str | None,
        context: MessageEditContext | None = None,
    ) -> tuple[TuiInputDrafts, TuiPendingInput]:
        identity = session_id or self.welcome_resource_id or new_id("ses")
        pending = TuiPendingInput(
            input_id=new_id("queued"),
            session_id=identity,
            origin_session_id=session_id,
            edit=context.target if context else None,
            text=content.text,
            attachments=content.attachments,
            skills=content.skills,
        )
        if session_id is None:
            drafts = self.model_copy(
                update={
                    "welcome": InputContent(),
                    "welcome_resource_id": identity,
                    "welcome_workspace": workspace,
                    "pending": (*self.pending, pending),
                }
            )
        else:
            drafts = (
                self._save_session(context.previous_draft)
                if context is not None
                else self.forget_sessions((session_id,))
            )
            drafts = drafts.model_copy(update={"pending": (*self.pending, pending)})
        return drafts, pending

    def finish_submission(self, input_id: str) -> TuiInputDrafts:
        return self.model_copy(
            update={"pending": tuple(item for item in self.pending if item.input_id != input_id)}
        )


class TuiInputDraftStore:
    """Read and atomically replace the TUI input-draft artifact."""

    def __init__(self, root: str | Path | None = None) -> None:
        root_value = root if root is not None else os.environ.get("OURO_HOME")
        ouro_home = Path(root_value) if root_value is not None else Path.home() / ".ouro"
        self.path = ouro_home / "config" / "tui-input-drafts.json"

    def load(self) -> TuiInputDrafts:
        if not self.path.exists():
            return TuiInputDrafts()
        try:
            raw = json.loads(read_text_atomic(self.path))
            return TuiInputDrafts.model_validate(raw)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"invalid TUI input drafts {self.path}: {exc}") from exc
        except OSError as exc:
            raise ConfigError(f"could not read TUI input drafts {self.path}: {exc}") from exc
        except ValidationError as exc:
            raise ConfigError(f"invalid TUI input drafts {self.path}: {exc}") from exc

    def save(self, drafts: TuiInputDrafts) -> None:
        write_model_atomic(self.path, drafts)


__all__ = [
    "MAX_SESSION_DRAFTS",
    "TuiEditTarget",
    "TuiInputDrafts",
    "TuiInputDraftStore",
    "TuiPendingInput",
    "TuiSessionDraft",
]
