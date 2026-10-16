"""Prepare both uploaded and referenced files without invoking Agent tools."""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import Literal

from ouro.core.attachment_projection import decode_slice, file_media_type, text_encoding
from ouro.core.base import utc_now
from ouro.core.content import (
    MEDIA_CONTENT_COST,
    AudioContent,
    MediaContent,
    MediaModality,
    VideoContent,
    media_format_omission,
    unsupported_content_text,
)
from ouro.core.input_content import InputFileReference
from ouro.core.message_roles import MessageRole
from ouro.core.messages import ContentBlock, ImageContent, MessageRecord, TextContent
from ouro.core.resources import ResourceRecord
from ouro.resources import ResourceStore
from ouro.tools.base import ToolExecutionContext

DEFAULT_INPUT_CONTENT_BUDGET = 8192
DEFAULT_REQUEST_MEDIA_CHARS = 20 * 1024 * 1024
DEFAULT_ITEM_TEXT_BYTES = 8 * 1024


def _media_budget_message(kind: MediaModality) -> str:
    return (
        f"The {kind} was not sent because there is too much media data to include at once. "
        "Use fewer files or smaller versions."
    )


Blocks = list[TextContent | MediaContent]


def with_attachment_read_paths(message: MessageRecord, paths: Iterable[Path]) -> MessageRecord:
    """Replace only the generated trailing path table, preserving observed file content."""
    paths = tuple(paths)
    if not paths:
        return message
    prefix = "\n\nSession attachment read paths:\n"
    content = list(message.content)
    if (
        message.semantic_tag == "attachments"
        and content
        and isinstance(content[-1], TextContent)
        and content[-1].text.startswith(prefix)
    ):
        content.pop()
    content.append(
        TextContent(
            text=prefix
            + "\n".join(
                f"{json.dumps(path.name, ensure_ascii=False)}: "
                f"{json.dumps(path.as_posix(), ensure_ascii=False)}"
                for path in paths
            )
        )
    )
    return message.model_copy(update={"content": content})


def _input_media_omission(
    role: MessageRole, kind: MediaModality, modalities: frozenset[MediaModality]
) -> str | None:
    if kind not in modalities:
        return unsupported_content_text(kind)
    if role not in {"user", "tool_result"}:
        return f"This {kind} cannot be shown here. Ask the user to attach it to their next message."
    return None


def _bound_media_content(
    content: list[ContentBlock],
    *,
    role: MessageRole,
    modalities: frozenset[MediaModality],
    media_budget: int,
) -> tuple[list[ContentBlock], int]:
    """Check input eligibility and charge inline media before reading resources."""
    bounded: list[ContentBlock] = []
    for block in content:
        if isinstance(block, MediaContent):
            reason = _input_media_omission(role, block.type, modalities)
            if reason is None and block.media_type:
                reason = media_format_omission(block.type, block.media_type)
            if reason is not None:
                bounded.append(TextContent(text=f"\n{reason}\n"))
                continue
            separator = block.uri.find(",") if block.uri.startswith("data:") else -1
            media_size = len(block.uri) - separator - 1 if separator >= 0 else len(block.uri)
            if media_size > media_budget:
                bounded.append(TextContent(text=f"\n{_media_budget_message(block.type)}\n"))
                continue
            media_budget -= media_size
        bounded.append(block)
    return bounded, media_budget


@dataclass(frozen=True)
class _Source:
    name: str
    path: Path
    kind: Literal["upload", "reference"]
    media_type: str
    size: int
    read: Callable[[tuple[tuple[int, int], ...]], tuple[bytes, ...]]

    def header(self) -> str:
        # Uploaded bytes are stable, but their Session address changes on copy/import.
        # Keep addresses in a separate, rebindable table; original references keep their path.
        label = "Session attachment" if self.kind == "upload" else "Original file reference"
        path = (
            f"Read path: {json.dumps(self.path.as_posix(), ensure_ascii=False)}\n"
            if self.kind == "reference"
            else f"Session file: {json.dumps(self.path.name, ensure_ascii=False)}\n"
        )
        return (
            f"File: {json.dumps(self.name, ensure_ascii=False)}\n"
            f"Source: {label}\n{path}"
            f"Media type: {self.media_type}; size: {self.size} bytes\n"
        )


class FileInputProjector:
    def __init__(
        self,
        *,
        store: ResourceStore,
        record: Callable[[str, str], ResourceRecord],
        max_resource_bytes: int,
        max_text_bytes: int,
        max_media_bytes: int,
    ) -> None:
        self.store, self.record = store, record
        self.max_resource_bytes, self.max_text_bytes, self.max_media_bytes = (
            max_resource_bytes,
            max_text_bytes,
            max_media_bytes,
        )

    def prepare_input(
        self,
        message: MessageRecord,
        *,
        input_modalities: frozenset[MediaModality],
        workspace: str | None,
        workspace_only: bool,
    ) -> MessageRecord:
        """Persist the bounded file material in the ordinary scaffold content once."""
        content = list(message.content)
        content_left = DEFAULT_INPUT_CONTENT_BUDGET
        media_left = DEFAULT_REQUEST_MEDIA_CHARS

        def omission(kind: MediaModality) -> str | None:
            if kind in input_modalities:
                return None
            return (
                f"The {kind} was not included when this file was attached because "
                "the model in use did not support it. Only the file information was provided."
            )

        context = ToolExecutionContext(workspace=workspace, workspace_only=workspace_only)
        for item in message.input_attachments:
            if isinstance(item, InputFileReference):
                try:
                    blocks, used, media_used = self._reference_blocks(
                        item,
                        context.resolve_path(item.path),
                        omission=omission,
                        content_budget=content_left,
                        media_budget=media_left,
                    )
                except (OSError, ValueError) as exc:
                    blocks, used, media_used = (
                        self._reference_only(
                            item, f"File unavailable: {exc}; contents not supplied"
                        ),
                        0,
                        0,
                    )
                content.append(
                    TextContent(
                        text=(
                            f"\n\nObserved at: {utc_now().isoformat()}\n"
                            "File contents were read at this time; "
                            "read the file again if you need its current contents.\n"
                        )
                    )
                )
            else:
                blocks, used, media_used = self._blocks(
                    self._upload_source(message.session_id, item.resource_id, item.name),
                    omission=omission,
                    content_budget=content_left,
                    media_budget=media_left,
                )
                content.append(TextContent(text="\n\n"))
            content.extend(blocks)
            content_left -= used
            media_left -= media_used
        return with_attachment_read_paths(
            message.model_copy(update={"content": content}),
            (
                self._upload_source(message.session_id, identity).path
                for identity in dict.fromkeys(message.resource_refs)
            ),
        )

    def _upload_source(self, session_id: str, resource_id: str, name: str | None = None) -> _Source:
        record = self.record(session_id, resource_id)
        return _Source(
            name or record.source.name,
            self.store.path_for(session_id, record),
            "upload",
            record.media_type,
            record.size,
            partial(self.store.read_ranges, session_id, record),
        )

    def _reference_blocks(
        self,
        item: InputFileReference,
        path: Path,
        *,
        omission: Callable[[MediaModality], str | None],
        content_budget: int,
        media_budget: int,
    ) -> tuple[Blocks, int, int]:
        for _attempt in range(2):
            if not path.is_file():
                raise OSError("Path is not a regular file")
            with path.open("rb") as stream:
                before = os.fstat(stream.fileno())

                def read(ranges: tuple[tuple[int, int], ...]) -> tuple[bytes, ...]:
                    parts = []
                    for offset, length in ranges:
                        stream.seek(offset)
                        parts.append(stream.read(length))
                    return tuple(parts)

                source = _Source(
                    item.name,
                    path,
                    "reference",
                    self.store.infer_media_type(path),
                    before.st_size,
                    read,
                )
                result = self._blocks(
                    source,
                    omission=omission,
                    content_budget=content_budget,
                    media_budget=media_budget,
                )
                after, current = os.fstat(stream.fileno()), path.stat()

                def key(info: os.stat_result) -> tuple[int, int, int, int]:
                    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)

                if key(before) == key(after) == key(current):
                    return result
        raise OSError("File changed while preparing this input; select it again")

    def project(
        self,
        messages: list[MessageRecord],
        *,
        input_modalities: frozenset[MediaModality],
        input_budget_tokens: int | None,
    ) -> list[MessageRecord]:
        """Prepare bounded input and resource views without placing tool output.

        Eligible tool media stay typed for provider-owned protocol placement.
        Only sendable inline media and resource previews consume the media budget.
        """
        projected = []
        content_left = (
            min(DEFAULT_INPUT_CONTENT_BUDGET, input_budget_tokens // 4)
            if input_budget_tokens is not None
            else DEFAULT_INPUT_CONTENT_BUDGET
        )
        media_left = DEFAULT_REQUEST_MEDIA_CHARS
        seen: set[tuple[str, str]] = set()
        for message in reversed(messages):
            omission = partial(_input_media_omission, message.role, modalities=input_modalities)
            content, media_left = _bound_media_content(
                message.content,
                role=message.role,
                modalities=input_modalities,
                media_budget=media_left,
            )
            if message.semantic_tag != "attachments":
                for identity in message.resource_refs:
                    source = self._upload_source(message.session_id, identity)
                    key = (message.session_id, identity)
                    if key in seen:
                        blocks, used, media_used = self._reference(
                            source, "Repeated attachment; see the more recent file entry"
                        )
                    else:
                        blocks, used, media_used = self._blocks(
                            source,
                            omission=omission,
                            content_budget=content_left,
                            media_budget=media_left,
                        )
                    content.append(TextContent(text="\n\n"))
                    content.extend(blocks)
                    content_left -= used
                    media_left -= media_used
                    seen.add(key)
            projected.append(
                with_attachment_read_paths(
                    message.model_copy(update={"content": content}),
                    (
                        self._upload_source(message.session_id, identity).path
                        for identity in dict.fromkeys(message.resource_refs)
                    ),
                )
            )
        return list(reversed(projected))

    @staticmethod
    def _reference_only(item: InputFileReference, reason: str) -> Blocks:
        return [
            TextContent(
                text=(
                    f"File: {json.dumps(item.name, ensure_ascii=False)}\n"
                    "Source: Original file reference\n"
                    f"Read path: {json.dumps(Path(item.path).as_posix(), ensure_ascii=False)}\n"
                    f"Representation: reference only. {reason}"
                )
            )
        ]

    @staticmethod
    def _reference(source: _Source, reason: str) -> tuple[Blocks, int, int]:
        return (
            [TextContent(text=source.header() + "Representation: reference only. " + reason)],
            0,
            0,
        )

    def _blocks(
        self,
        source: _Source,
        *,
        omission: Callable[[MediaModality], str | None],
        content_budget: int,
        media_budget: int,
    ) -> tuple[Blocks, int, int]:
        if source.size > self.max_resource_bytes:
            return self._reference(
                source,
                "The file is too large to include automatically. "
                "Read a smaller portion or provide a smaller file.",
            )
        preview_budget = min(content_budget, DEFAULT_ITEM_TEXT_BYTES, self.max_text_bytes)
        edge = max(512, preview_budget // 2)
        full = source.size <= preview_budget
        ranges = ((0, source.size),) if full else ((0, edge), (max(edge, source.size - edge), edge))
        parts = source.read(ranges)
        prefix = parts[0]
        detected = file_media_type(prefix)
        hinted_kind = source.media_type.partition("/")[0]
        if detected is not None or hinted_kind in {"image", "audio", "video"}:
            if detected is None:
                return self._reference(source, "The file could not be read as supported media.")
            source = replace(source, media_type=detected)
            constructors: dict[
                MediaModality, type[ImageContent] | type[AudioContent] | type[VideoContent]
            ] = {"image": ImageContent, "audio": AudioContent, "video": VideoContent}
            kind: MediaModality = next(
                kind for kind in constructors if detected.startswith(kind + "/")
            )
            reason = omission(kind) or media_format_omission(kind, detected)
            if reason is not None:
                return self._reference(source, reason)
            encoded_size = 4 * ((source.size + 2) // 3)
            if source.size > self.max_media_bytes:
                return self._reference(
                    source, f"The {kind} is too large to send. Use a smaller version."
                )
            cost = MEDIA_CONTENT_COST
            if encoded_size > media_budget or cost > content_budget:
                return self._reference(source, _media_budget_message(kind))
            raw = prefix if full else source.read(((0, source.size),))[0]
            return (
                [
                    TextContent(text=source.header()),
                    constructors[kind](
                        uri=f"data:{detected};base64,{base64.b64encode(raw).decode('ascii')}",
                        media_type=detected,
                    ),
                ],
                cost,
                encoded_size,
            )
        text_budget = preview_budget
        encoding = text_encoding(prefix)
        if encoding is None:
            return self._reference(
                source, "Binary file or unsupported text encoding; contents not supplied"
            )
        if text_budget < 1024 and source.size > text_budget:
            return self._reference(
                source,
                "The file's text was not included because there is too much text to include at "
                "once. Read the relevant portion of the file if needed.",
            )

        def decode_windows(windows: tuple[tuple[bytes, int], ...]) -> list[tuple[str, int, int]]:
            return [
                decode_slice(part, encoding, offset=start, final=start + len(part) >= source.size)
                for part, start in windows
            ]

        try:
            slices = decode_windows(
                tuple((part, start) for part, (start, _) in zip(parts, ranges, strict=True))
            )
            used = sum(len(text.encode("utf-8")) for text, _, _ in slices)
            if used > text_budget:
                # UTF-16 may expand to 3 UTF-8 bytes per 2 source bytes. Reuse
                # the bounded windows already read and keep both file edges.
                edge = text_budget // 3
                tail = parts[-1][-edge:]
                slices = decode_windows(((parts[0][:edge], 0), (tail, source.size - len(tail))))
                used = sum(len(text.encode("utf-8")) for text, _, _ in slices)
                full = False
        except UnicodeDecodeError:
            return self._reference(source, "Unsupported text encoding; contents not supplied")
        rendered = []
        for index, (text, start, end) in enumerate(slices):
            if index:
                rendered.append(
                    f"[omitted bytes {slices[index - 1][2]}..{start}; read the file path for more]"
                )
            rendered.append(f"[bytes {start}..{end}, end exclusive]\n{text}")
        description = "full text" if full else "head/tail preview"
        return (
            [
                TextContent(
                    text=source.header()
                    + f"Encoding: {encoding}\nRepresentation: {description}\n"
                    + "\n".join(rendered)
                )
            ],
            used,
            0,
        )
