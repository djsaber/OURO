"""Bounded HTTP and raster validation for the optional media integrations."""

import asyncio
import json
import struct
from io import BytesIO

import httpx
from PIL import Image, ImageFile

from .files import WorkbenchError

JSON_TYPES = frozenset({"application/json"})
RASTER_TYPES = {
    "JPEG": ("jpg", "image/jpeg"),
    "PNG": ("png", "image/png"),
    "WEBP": ("webp", "image/webp"),
}
MAX_PIXELS = 25_000_000


async def fetch_bytes(
    client: httpx.AsyncClient,
    request: httpx.Request,
    *,
    service: str,
    max_bytes: int,
    content_types: frozenset[str],
    deadline: float,
) -> bytes:
    # A wall-clock deadline also bounds servers that slowly drip feed a response.
    try:
        async with asyncio.timeout(deadline):
            response = await client.send(request, stream=True, follow_redirects=False)
            try:
                if not response.is_success:
                    raise WorkbenchError(f"{service} returned HTTP {response.status_code}.")
                content_type = response.headers.get("content-type", "").partition(";")[0]
                if content_type.strip().lower() not in content_types:
                    raise WorkbenchError(f"{service} returned an unexpected content type.")
                # Don't let transparent decompression inflate data before the size check.
                encoding = response.headers.get("content-encoding", "identity")
                if encoding.strip().lower() != "identity":
                    raise WorkbenchError(f"{service} returned an unsupported content encoding.")
                length = response.headers.get("content-length")
                if length is not None and (
                    not length.isascii()
                    or not length.isdecimal()
                    or len(length) > 12
                    or int(length) > max_bytes
                ):
                    raise WorkbenchError(f"{service} returned an invalid or oversized response.")
                body = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                    if len(body) + len(chunk) > max_bytes:
                        raise WorkbenchError(f"{service} response exceeds the size limit.")
                    body.extend(chunk)
                return bytes(body)
            finally:
                await response.aclose()
    except httpx.HTTPError, TimeoutError:
        pass
    # Don't propagate provider URLs, bodies, or request objects in error messages or causes.
    raise WorkbenchError(f"{service} request failed or timed out.")


def json_object(body: bytes, service: str) -> dict[str, object]:
    try:
        value = json.loads(body)
        if isinstance(value, dict):
            return value
    except ValueError, RecursionError:
        pass
    raise WorkbenchError(f"{service} returned invalid JSON metadata.")


def raster_type(data: bytes, description: str) -> tuple[str, str]:
    try:
        with Image.open(BytesIO(data), formats=tuple(RASTER_TYPES)) as raster:
            if not (
                isinstance(raster, ImageFile.ImageFile)
                and raster.format in RASTER_TYPES
                and 0 < raster.width * raster.height <= MAX_PIXELS
                and getattr(raster, "n_frames", 1) == 1
            ):
                raise WorkbenchError(
                    f"{description} must be a single image within the pixel limit."
                )
            kind = RASTER_TYPES[raster.format]
            raster.verify()
        with Image.open(BytesIO(data), formats=tuple(RASTER_TYPES)) as raster:
            raster.load()
        return kind
    except WorkbenchError:
        raise
    except (
        OSError,
        ValueError,
        SyntaxError,
        EOFError,
        struct.error,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ):
        pass
    raise WorkbenchError(f"{description} must be a valid JPEG, PNG, or WebP image.")
