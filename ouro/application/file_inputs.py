"""Prepare both uploaded and referenced files without invoking Agent tools."""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Literal

from ouro.core.attachment_projection import decode_slice, image_media_type, text_encoding
from ouro.core.base import utc_now
from ouro.core.content import unsupported_content_text
from ouro.core.input_content import InputFileReference
from ouro.core.message_roles import MessageRole
from ouro.core.messages import ContentBlock, ImageContent, MessageRecord, TextContent
from ouro.core.resources import ResourceRecord
from ouro.resources import ResourceStore
from ouro.tools.base import ToolExecutionContext

DEFAULT_INPUT_CONTENT_BUDGET = 8192
IMAGE_CONTENT_COST = 2048
DEFAULT_REQUEST_IMAGE_CHARS = 20 * 1024 * 1024
DEFAULT_ITEM_TEXT_BYTES = 8 * 1024

_IMAGE_BUDGET_MESSAGE = (
    "The image was not sent because there is too much image data to include at once. "
    "Use fewer images or smaller versions."
)

Blocks = list[TextContent | ImageContent]


def _input_image_omission(role: MessageRole, supports_vision: bool) -> str | None:
    # Tool results retain their content for provider-owned placement and fallback.
    if role == "tool_result":
        return None
    if not supports_vision:
        return unsupported_content_text("image")
    if role != "user":
        return "This image cannot be shown here. Ask the user to attach it to their next message."
    return None


def _bound_image_content(
    content: list[ContentBlock], *, omission: str | None, image_budget: int
) -> tuple[list[ContentBlock], int]:
    """Apply input eligibility and charge inline images before materializing resources."""
    bounded: list[ContentBlock] = []
    for block in content:
        if isinstance(block, ImageContent):
            separator = block.uri.find(",")
            image_size = len(block.uri) - separator - 1 if separator >= 0 else 0
            reason = omission or (_IMAGE_BUDGET_MESSAGE if image_size > image_budget else None)
            if reason is not None:
                bounded.append(TextContent(text=f"\n{reason}\n"))
                continue
            image_budget -= image_size
        bounded.append(block)
    return bounded, image_budget


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
        # Resolve that address at request construction; original references keep their path.
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
        max_image_bytes: int,
    ) -> None:
        self.store, self.record = store, record
        self.max_resource_bytes, self.max_text_bytes, self.max_image_bytes = (
            max_resource_bytes,
            max_text_bytes,
            max_image_bytes,
        )

    def prepare_input(
        self,
        message: MessageRecord,
        *,
        supports_vision: bool,
        workspace: str | None,
        workspace_only: bool,
    ) -> MessageRecord:
        """Persist the bounded file material in the ordinary scaffold content once."""
        content = list(message.content)
        text_left = DEFAULT_INPUT_CONTENT_BUDGET
        images_left = DEFAULT_REQUEST_IMAGE_CHARS
        # This explanation is persisted with the observation, so it must not
        # describe the capabilities of a later request or a different model.
        image_omission = (
            None
            if supports_vision
            else "The image was not included when this file was attached because the model "
            "in use could not view images. Only the file information was provided."
        )
        context = ToolExecutionContext(workspace=workspace, workspace_only=workspace_only)
        for item in message.input_attachments:
            text_budget = min(text_left, DEFAULT_ITEM_TEXT_BYTES, self.max_text_bytes)
            image_budget = images_left if text_left >= IMAGE_CONTENT_COST else 0
            if isinstance(item, InputFileReference):
                try:
                    blocks, used, image_used = self._reference_blocks(
                        item,
                        context.resolve_path(item.path),
                        image_omission=image_omission,
                        text_budget=text_budget,
                        image_budget=image_budget,
                    )
                except (OSError, ValueError) as exc:
                    blocks, used, image_used = (
                        self._reference_only(
                            item, f"File unavailable: {exc}; contents and pixels not supplied"
                        ),
                        0,
                        0,
                    )
                content.append(
                    TextContent(
                        text=(
                            f"\n\nObserved at: {utc_now().isoformat()}\n"
                            "Original-file contents at input consumption; "
                            "later tool reads may see changes.\n"
                        )
                    )
                )
            else:
                blocks, used, image_used = self._blocks(
                    self._upload_source(message.session_id, item.resource_id, item.name),
                    image_omission=image_omission,
                    text_budget=text_budget,
                    image_budget=image_budget,
                )
                content.append(TextContent(text="\n\n"))
            content.extend(blocks)
            text_left -= used + (IMAGE_CONTENT_COST if image_used else 0)
            images_left -= image_used
        return message.model_copy(update={"content": content})

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
        image_omission: str | None,
        text_budget: int,
        image_budget: int,
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
                    image_omission=image_omission,
                    text_budget=text_budget,
                    image_budget=image_budget,
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
        supports_vision: bool,
        input_budget_tokens: int | None,
    ) -> list[MessageRecord]:
        """Prepare bounded input and resource views without placing tool output.

        Input images follow input eligibility; tool images stay typed for the codec.
        Every inline image and resource preview shares the request image budget.
        """
        projected = []
        text_left = (
            min(DEFAULT_INPUT_CONTENT_BUDGET, input_budget_tokens // 4)
            if input_budget_tokens is not None
            else DEFAULT_INPUT_CONTENT_BUDGET
        )
        images_left = DEFAULT_REQUEST_IMAGE_CHARS
        seen: set[tuple[str, str]] = set()
        for message in reversed(messages):
            image_omission = _input_image_omission(message.role, supports_vision)
            content, images_left = _bound_image_content(
                message.content, omission=image_omission, image_budget=images_left
            )
            if message.semantic_tag != "attachments":
                for identity in message.resource_refs:
                    source = self._upload_source(message.session_id, identity)
                    key = (message.session_id, identity)
                    if key in seen:
                        blocks, used, image_used = self._reference(
                            source, "Repeated attachment; see the more recent file entry"
                        )
                    else:
                        blocks, used, image_used = self._blocks(
                            source,
                            image_omission=image_omission,
                            text_budget=min(
                                text_left, DEFAULT_ITEM_TEXT_BYTES, self.max_text_bytes
                            ),
                            image_budget=images_left if text_left >= IMAGE_CONTENT_COST else 0,
                        )
                    content.append(TextContent(text="\n\n"))
                    content.extend(blocks)
                    text_left -= used + (IMAGE_CONTENT_COST if image_used else 0)
                    images_left -= image_used
                    seen.add(key)
            if message.resource_refs:
                paths = []
                for identity in dict.fromkeys(message.resource_refs):
                    source = self._upload_source(message.session_id, identity)
                    paths.append(
                        f"{json.dumps(source.path.name, ensure_ascii=False)}: "
                        f"{json.dumps(source.path.as_posix(), ensure_ascii=False)}"
                    )
                content.append(
                    TextContent(text="\n\nSession attachment read paths:\n" + "\n".join(paths))
                )
            projected.append(message.model_copy(update={"content": content}))
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
        image_omission: str | None,
        text_budget: int,
        image_budget: int,
    ) -> tuple[Blocks, int, int]:
        if source.size > self.max_resource_bytes:
            return self._reference(
                source,
                "The file is too large to include automatically. "
                "Read a smaller portion or provide a smaller file.",
            )
        edge = max(512, text_budget // 2)
        full = source.size <= text_budget
        ranges = ((0, source.size),) if full else ((0, edge), (max(edge, source.size - edge), edge))
        parts = source.read(ranges)
        prefix = parts[0]
        detected_image = image_media_type(prefix)
        if detected_image is not None or source.media_type.startswith("image/"):
            if detected_image is None:
                return self._reference(
                    source,
                    "The file could not be read as an image. "
                    "Provide a valid PNG, JPEG, GIF, or WebP image.",
                )
            if image_omission is not None:
                return self._reference(source, image_omission)
            encoded_size = 4 * ((source.size + 2) // 3)
            if source.size > self.max_image_bytes:
                return self._reference(
                    source, "The image is too large to send. Use a smaller version."
                )
            if encoded_size > image_budget:
                return self._reference(source, _IMAGE_BUDGET_MESSAGE)
            raw = prefix if full else source.read(((0, source.size),))[0]
            return (
                [
                    TextContent(text=source.header()),
                    ImageContent(
                        uri=f"data:{detected_image};base64,{base64.b64encode(raw).decode('ascii')}",
                        media_type=detected_image,
                    ),
                ],
                0,
                encoded_size,
            )
        encoding = text_encoding(prefix)
        if encoding is None:
            return self._reference(
                source, "Opaque binary or unsupported text encoding; contents not supplied"
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
