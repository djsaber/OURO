from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Literal

from pydantic import ConfigDict, Field, model_validator

from ouro.core.base import Metadata, MetadataField, VersionedOuroModel, new_id, utc_now
from ouro.core.billing import provider_usage_entry_id
from ouro.core.context import ContextMetrics, ProviderContext
from ouro.core.errors import PersistenceError
from ouro.core.messages import MessageRecord
from ouro.core.request_manifest import RequestManifest


class ModelRequestHeader(VersionedOuroModel):
    """Request identity and metrics without provider arguments or media bodies."""

    schema_version: int = 11

    context_id: str = Field(
        default_factory=lambda: new_id("ctx"), pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$"
    )
    session_id: str
    run_id: str
    purpose: Literal["agent", "compaction"] = "agent"
    turn_id: str
    model_round: int = Field(ge=1)
    provider_id: str
    endpoint_id: str | None = None
    model_id: str
    prepared_at: datetime = Field(default_factory=utc_now)
    started_at: datetime | None = None
    provider_done_at: datetime | None = None
    interrupted_at: datetime | None = None
    response_message_id: str | None = None
    response_compaction_id: str | None = None
    response_closed_at: datetime | None = None
    usage_entry_id: str | None = None
    metrics: ContextMetrics
    context_metadata: Metadata = MetadataField

    @model_validator(mode="after")
    def _validate_identity_and_completion(self) -> ModelRequestHeader:
        if not all(
            value.strip()
            for value in (
                self.context_id,
                self.session_id,
                self.run_id,
                self.turn_id,
                self.provider_id,
                self.model_id,
            )
        ):
            raise ValueError("context snapshot identity fields must not be empty")
        if self.provider_done_at is not None and self.interrupted_at is not None:
            raise ValueError("request cannot settle as both provider done and interrupted")
        if self.usage_entry_id is not None:
            if self.provider_done_at is None and self.interrupted_at is None:
                raise ValueError("usage reference requires request settlement")
            if self.usage_entry_id != self.expected_usage_entry_id:
                raise ValueError("usage reference conflicts with request identity")
        return self

    @property
    def expected_usage_entry_id(self) -> str:
        return provider_usage_entry_id(
            run_id=self.run_id,
            request_id=self.turn_id,
            purpose=self.purpose,
            provider_id=self.provider_id,
            endpoint_id=self.endpoint_id,
            model_id=self.model_id,
        )

    def settled(
        self,
        *,
        context_id: str,
        settled_at: datetime,
        provider_done: bool,
        usage_entry_id: str | None,
    ) -> ModelRequestHeader:
        """Settle this exact request without consulting its immutable input."""
        if self.context_id != context_id:
            raise PersistenceError(f"latest context does not match {context_id}")
        if self.provider_done_at is not None or self.interrupted_at is not None:
            if (self.provider_done_at is not None) != provider_done or (
                self.usage_entry_id is not None and self.usage_entry_id != usage_entry_id
            ):
                raise PersistenceError("context completion conflicts with its recorded usage")
            return (
                ModelRequestHeader.model_validate(
                    {
                        **self.model_dump(),
                        "usage_entry_id": usage_entry_id,
                    }
                )
                if self.usage_entry_id != usage_entry_id
                else self
            )
        return ModelRequestHeader.model_validate(
            {
                **self.model_dump(),
                "provider_done_at" if provider_done else "interrupted_at": settled_at,
                "usage_entry_id": usage_entry_id,
            }
        )


class RequestManifestRecord(VersionedOuroModel):
    """Immutable final skeleton, stored separately from its completion header."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 10
    context_id: str

    supports_image_input: bool = False
    supports_audio_input: bool = False
    supports_video_input: bool = False
    manifest: RequestManifest


class ContextSnapshotRecord(ModelRequestHeader):
    """Explicit full read of a latest request's header and input."""

    supports_image_input: bool = False
    supports_audio_input: bool = False
    supports_video_input: bool = False
    provider_context: ProviderContext

    def header(self) -> ModelRequestHeader:
        return ModelRequestHeader.model_validate(
            self.model_dump(include=set(ModelRequestHeader.model_fields))
        )

    @classmethod
    def assemble(
        cls,
        header: ModelRequestHeader,
        body: RequestManifestRecord,
        sources: Mapping[str, MessageRecord],
    ) -> ContextSnapshotRecord:
        if header.context_id != body.context_id:
            raise PersistenceError("context header and input have different identities")
        return cls(
            **header.model_dump(),
            supports_image_input=body.supports_image_input,
            supports_audio_input=body.supports_audio_input,
            supports_video_input=body.supports_video_input,
            provider_context=ProviderContext(
                system_prompt=body.manifest.system_prompt,
                messages=body.manifest.materialize(header.session_id, sources),
                tools=body.manifest.tools,
            ),
        )


def validate_request_snapshot(
    session_id: str,
    headers: Sequence[ModelRequestHeader],
    manifest: RequestManifestRecord | None,
) -> None:
    by_id = {header.context_id: header for header in headers}
    if len(by_id) != len(headers) or any(header.session_id != session_id for header in headers):
        raise PersistenceError("snapshot requests contain duplicate or foreign identities")
    if manifest is not None:
        header = by_id.get(manifest.context_id)
        if header is None or header.purpose != "agent":
            raise PersistenceError("snapshot input references a missing or non-agent request")


__all__ = ["ModelRequestHeader", "RequestManifestRecord", "ContextSnapshotRecord"]
