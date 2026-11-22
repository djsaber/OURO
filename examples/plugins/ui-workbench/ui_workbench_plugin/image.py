"""Opt-in image generation and editing through an OpenAI-compatible HTTPS API.

The API base must serve /images/generations (JSON) and/or /images/edits
(single-image multipart). The model must return one inline data[0].b64_json
image by default, as GPT Image models do. URL-only results are not supported.
"""

import base64
import binascii
import re
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .files import WorkbenchError, read_limited, workspace_file
from .http import JSON_TYPES, fetch_bytes, json_object, raster_type

_SERVICE = "Image provider"
_MAX_PROMPT = 4_000
_MAX_PATH = 1_024
_MAX_INPUT_BYTES = 8 * 1024 * 1024
_MAX_OUTPUT_BYTES = 12 * 1024 * 1024
_MAX_ENCODED = 4 * ((_MAX_OUTPUT_BYTES + 2) // 3)
_BASE_PATH = re.compile(r"(?:/[a-zA-Z0-9._~-]+)*/?")
_SIZE_OPTIONS = ("auto", "1024x1024", "1536x1024", "1024x1536")
_BACKGROUND_OPTIONS = ("auto", "opaque", "transparent")

IMAGE_PARAMETERS = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["generate", "edit"]},
        "prompt": {"type": "string", "minLength": 1, "maxLength": _MAX_PROMPT},
        "path": {"type": "string", "minLength": 1, "maxLength": _MAX_PATH},
        "size": {"type": "string", "enum": list(_SIZE_OPTIONS)},
        "background": {"type": "string", "enum": list(_BACKGROUND_OPTIONS)},
    },
    "required": ["action", "prompt"],
    "additionalProperties": False,
    "oneOf": [
        {"properties": {"action": {"const": "generate"}}, "not": {"required": ["path"]}},
        {"properties": {"action": {"const": "edit"}}, "required": ["path"]},
    ],
}


def _api_url(endpoint: str, route: str) -> httpx.URL:
    if (
        len(endpoint) <= 1_024
        and endpoint.startswith("https://")
        and all(33 <= ord(char) <= 126 for char in endpoint)
        and not any(char in endpoint for char in "\\?#%")
    ):
        try:
            parsed = urlsplit(endpoint)
            if (
                parsed.hostname
                and "@" not in parsed.netloc
                and _BASE_PATH.fullmatch(parsed.path)
                and not any(part in {".", ".."} for part in parsed.path.split("/"))
                and (parsed.port is None or parsed.port > 0)
            ):
                return httpx.URL(f"{endpoint.rstrip('/')}/images/{route}")
        except ValueError, httpx.InvalidURL:
            pass
    raise WorkbenchError(
        "Configure a valid HTTPS image API base URL without credentials or a query."
    )


def _inline_image(response: dict[str, object]) -> bytes:
    entries = response.get("data")
    if not isinstance(entries, list) or len(entries) != 1 or not isinstance(entries[0], dict):
        raise WorkbenchError("Image provider must return exactly one image.")
    encoded = entries[0].get("b64_json")
    if not isinstance(encoded, str) or not encoded:
        raise WorkbenchError("Image provider must return inline base64 image data, not a URL.")
    if len(encoded) > _MAX_ENCODED:
        raise WorkbenchError("Image provider output exceeds the image size limit.")
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except ValueError, binascii.Error:
        pass
    else:
        if len(decoded) > _MAX_OUTPUT_BYTES:
            raise WorkbenchError("Image provider output exceeds the image size limit.")
        return decoded
    raise WorkbenchError("Image provider returned invalid base64 image data.")


async def image(
    workspace: Path | None,
    arguments: Mapping[str, object],
    *,
    endpoint: str,
    model: str,
    api_key: str,
) -> tuple[dict[str, bytes], dict[str, object]]:
    """Return one validated raster in memory; caller owns approval and publication."""
    action = arguments.get("action")
    if action not in ("generate", "edit"):
        raise WorkbenchError("Image action must be generate or edit.")
    allowed = {"action", "prompt", "size", "background"}
    if action == "edit":
        allowed.add("path")
    if arguments.keys() - allowed:
        raise WorkbenchError("Unsupported image arguments; only edits take a workspace image path.")
    prompt = arguments.get("prompt")
    if (
        not isinstance(prompt, str)
        or not 1 <= len(prompt) <= _MAX_PROMPT
        or not prompt.strip()
        or not all(char.isprintable() or char in "\t\r\n" for char in prompt)
    ):
        raise WorkbenchError(f"Provide a nonempty prompt of at most {_MAX_PROMPT} characters.")
    fields = {"model": model, "prompt": prompt}
    for key, choices in (("size", _SIZE_OPTIONS), ("background", _BACKGROUND_OPTIONS)):
        if key in arguments:
            option = arguments[key]
            if not isinstance(option, str) or option not in choices:
                raise WorkbenchError(f"Image {key} must be one of: {', '.join(choices)}.")
            fields[key] = option
    if not 1 <= len(model) <= 256 or not all(33 <= ord(char) <= 126 for char in model):
        raise WorkbenchError("Configure a valid image model name.")
    if not 1 <= len(api_key) <= 4_096 or not all(33 <= ord(char) <= 126 for char in api_key):
        raise WorkbenchError("Configure a valid image API credential.")
    url = _api_url(endpoint, "edits" if action == "edit" else "generations")
    upload = None
    if action == "edit":
        path = arguments.get("path")
        if workspace is None:
            raise WorkbenchError("Image edits require a workspace.")
        if not isinstance(path, str) or not all(char.isprintable() for char in path):
            raise WorkbenchError("Provide a workspace-relative image path for editing.")
        input_bytes = read_limited(workspace_file(workspace, path), _MAX_INPUT_BYTES)
        extension, mime = raster_type(input_bytes, "Workspace image")
        upload = {"image": (f"input.{extension}", input_bytes, mime)}
    async with httpx.AsyncClient(
        trust_env=False,
        timeout=httpx.Timeout(120, connect=10),
        headers={
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Authorization": f"Bearer {api_key}",
        },
    ) as client:
        if upload is None:
            request = client.build_request("POST", url, json={**fields, "n": 1})
        else:
            request = client.build_request("POST", url, data={**fields, "n": "1"}, files=upload)
        body = await fetch_bytes(
            client,
            request,
            service=_SERVICE,
            max_bytes=_MAX_ENCODED + 64 * 1024,
            content_types=JSON_TYPES,
            deadline=120,
        )
    output = _inline_image(json_object(body, _SERVICE))
    extension, mime = raster_type(output, "Image provider output")
    filename = f"{'edited' if action == 'edit' else 'generated'}.{extension}"
    return {filename: output}, {"action": action, "filename": filename, "media_type": mime}
