"""Bounded local image inspection through the ordinary tool-result contract."""

from __future__ import annotations

import base64
import json
import os
import stat
from collections.abc import Buffer
from io import BytesIO
from pathlib import Path
from typing import Literal

from pydantic import Field

from ouro.core.base import utc_now
from ouro.core.content import ImageContent, TextContent
from ouro.tools.base import Tool, ToolDefinition, ToolExecutionContext, ToolInvocation, ToolResult
from ouro.tools.builtin_common import BuiltinArguments

_MAX_INPUT_BYTES = 32 * 1024 * 1024
_MAX_PIXELS = 40_000_000
_HIGH_MAX_DIMENSION = 2048
_FORMATS = ("PNG", "JPEG", "WEBP", "GIF", "BMP", "TIFF")
_NATIVE_MEDIA_TYPES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}
_OUTPUT_LIMIT_MESSAGE = (
    "image exceeds the tool media output limit; use detail='high' or a smaller image"
)


class _ImageOutput(BytesIO):
    """Stop PNG encoding before retaining bytes that cannot fit in the result."""

    def __init__(self, media_chars: int):
        super().__init__()
        self.max_bytes = max(0, (media_chars - len("data:image/png;base64,")) // 4 * 3)

    def write(self, data: Buffer, /) -> int:
        if self.tell() + memoryview(data).nbytes > self.max_bytes:
            raise ValueError(_OUTPUT_LIMIT_MESSAGE)
        return super().write(data)


class _ViewImageArguments(BuiltinArguments):
    path: str = Field(min_length=1, description="Local filesystem path to an image file.")
    detail: Literal["high", "original"] = Field(
        default="high",
        description=(
            "Image detail level. Defaults to `high`; use `original` to preserve exact resolution."
        ),
    )


def builtin_view_image_tool() -> Tool:
    return Tool(
        definition=ToolDefinition(
            name="ViewImage",
            description=(
                "View a local image file from the filesystem when visual inspection is needed. "
                "Use this for images already available on disk."
            ),
            parameters_schema=_ViewImageArguments.model_json_schema(),
            permission_mode="auto",
            effect="read_only",
        ),
        handler=_view_image,
    )


def _view_image(invocation: ToolInvocation, context: ToolExecutionContext) -> ToolResult:
    arguments = _ViewImageArguments.model_validate(invocation.arguments)
    context.raise_if_aborted()
    path = context.resolve_path(arguments.path, read_only=True)
    raw = _read_image(path)
    context.raise_if_aborted()
    # Load the decoder only when needed; registration and file checks stay lightweight.
    from PIL import Image, ImageOps, UnidentifiedImageError

    try:
        with Image.open(BytesIO(raw), formats=_FORMATS) as source:
            original_size = source.size
            if source.width * source.height > _MAX_PIXELS:
                raise ValueError(f"image exceeds the {_MAX_PIXELS:,}-pixel inspection limit")
            first_frame = bool(getattr(source, "is_animated", False))
            orientation = source.getexif().get(274, 1)
            media_type = _NATIVE_MEDIA_TYPES.get(source.format or "")
            resized = arguments.detail == "high" and max(source.size) > _HIGH_MAX_DIMENSION
            if media_type is not None and not first_frame and orientation == 1 and not resized:
                # Opening identifies headers only. Validate unchanged bytes by
                # decoding the selected frame before exposing it as image content.
                source.load()
                encoded, size = raw, source.size
            else:
                context.raise_if_aborted()
                if resized:
                    # Let thumbnail use decoder reduction (notably JPEG draft)
                    # before loading full-size pixels. The square bound is rotation invariant.
                    source.thumbnail(
                        (_HIGH_MAX_DIMENSION, _HIGH_MAX_DIMENSION), Image.Resampling.LANCZOS
                    )
                ImageOps.exif_transpose(source, in_place=True)
                size = source.size
                # A static PNG carries transparency and normalizes raster formats
                # that model endpoints do not accept directly. Never write to disk.
                with (
                    source.convert("RGBA") as normalized,
                    _ImageOutput(context.max_output_media_chars) as output,
                ):
                    normalized.save(output, format="PNG")
                    encoded = output.getvalue()
                media_type = "image/png"
    except (UnidentifiedImageError, OSError, SyntaxError) as error:
        raise ValueError("unable to process image: invalid or unsupported image data") from error
    context.raise_if_aborted()
    prefix = f"data:{media_type};base64,"
    if len(prefix) + 4 * ((len(encoded) + 2) // 3) > context.max_output_media_chars:
        raise ValueError(_OUTPUT_LIMIT_MESSAGE)
    description = (
        f"Image: {json.dumps(str(path), ensure_ascii=False)}\n"
        f"Resolution: {size[0]} x {size[1]}; detail: {arguments.detail}."
    )
    if first_frame:
        description += " Only the first frame is supplied."
    return ToolResult(
        tool_call_id=invocation.tool_call_id,
        tool_name=invocation.tool_name,
        content=[
            TextContent(text=description),
            ImageContent(
                uri=prefix + base64.b64encode(encoded).decode("ascii"), media_type=media_type
            ),
        ],
        structured={
            "path": str(path),
            "detail": arguments.detail,
            "original_width": original_size[0],
            "original_height": original_size[1],
            "width": size[0],
            "height": size[1],
            "resized": resized,
            "first_frame": first_frame,
            "media_type": media_type,
            "source_bytes": len(raw),
            "output_bytes": len(encoded),
        },
        ended_at=utc_now(),
    )


def _read_image(path: Path) -> bytes:
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError(f"image path is not a regular file: {path}")
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"image path is not a regular file: {path}")
        if before.st_size > _MAX_INPUT_BYTES:
            raise ValueError("image exceeds the 32 MiB input limit")
        raw = stream.read(_MAX_INPUT_BYTES + 1)
        after = os.fstat(stream.fileno())
    if len(raw) > _MAX_INPUT_BYTES:
        raise ValueError("image exceeds the 32 MiB input limit")
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("image changed while being read; retry the call")
    return raw
