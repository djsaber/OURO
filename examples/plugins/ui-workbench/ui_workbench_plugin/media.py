"""Search the Met Open Access collection and explicitly download an eligible image."""

import asyncio
import json
import re
from collections.abc import Mapping
from typing import TypeGuard

import httpx

from .files import WorkbenchError
from .http import JSON_TYPES, RASTER_TYPES, fetch_bytes, json_object, raster_type

_API = "https://collectionapi.metmuseum.org/public/collection/v1"
_SOURCE = "The Metropolitan Museum of Art"
_RIGHTS_URL = "https://www.metmuseum.org/about-the-met/policies-and-documents/open-access"
_LICENSE_URL = "https://creativecommons.org/publicdomain/zero/1.0/"
_IMAGE_URL = re.compile(
    r"https://images\.metmuseum\.org/CRDImages/[a-zA-Z0-9_-]+/"
    r"(?:original|web-large|mobile-large)/[a-zA-Z0-9_.-]+\.(?:jpe?g|png|webp)",
    re.IGNORECASE | re.ASCII,
)
_IMAGE_TYPES = frozenset(mime for _, mime in RASTER_TYPES.values())
_MAX_ID = 2_147_483_647
_SEARCH_CANDIDATES = 20
_MAX_LIMIT = 10
_MAX_QUERY = 160
_MAX_ASSET_BYTES = 16 * 1024 * 1024

MEDIA_PARAMETERS = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["search", "download"]},
        "query": {"type": "string", "minLength": 1, "maxLength": _MAX_QUERY},
        "limit": {"type": "integer", "minimum": 1, "maximum": _MAX_LIMIT, "default": 5},
        "item_id": {"type": "integer", "minimum": 1, "maximum": _MAX_ID},
    },
    "required": ["action"],
    "additionalProperties": False,
    "oneOf": [
        {
            "properties": {"action": {"const": "search"}},
            "required": ["query"],
            "not": {"required": ["item_id"]},
        },
        {
            "properties": {"action": {"const": "download"}},
            "required": ["item_id"],
            "not": {"anyOf": [{"required": ["query"]}, {"required": ["limit"]}]},
        },
    ],
}


def _item_id(value: object) -> TypeGuard[int]:
    return type(value) is int and 1 <= value <= _MAX_ID


def _image_url(value: object) -> str | None:
    if isinstance(value, str) and len(value) <= 512 and _IMAGE_URL.fullmatch(value):
        return value
    return None


def _text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    # Avoid control characters and lone surrogates in human-readable attribution.
    return " ".join("".join(char if char.isprintable() else " " for char in value[:500]).split())


async def _get_json(
    client: httpx.AsyncClient,
    path: str,
    *,
    params: dict[str, str] | None = None,
) -> dict[str, object]:
    body = await fetch_bytes(
        client,
        client.build_request("GET", _API + path, params=params),
        service=_SOURCE,
        max_bytes=1024 * 1024,
        content_types=JSON_TYPES,
        deadline=8,
    )
    return json_object(body, _SOURCE)


async def _eligible_item(
    client: httpx.AsyncClient, item_id: int
) -> tuple[dict[str, object], str] | None:
    data = await _get_json(client, f"/objects/{item_id}")
    returned_id = data.get("objectID")
    if type(returned_id) is not int or returned_id != item_id:
        raise WorkbenchError("The Met returned a mismatched collection item.")
    if data.get("isPublicDomain") is not True:
        return None
    asset_url = _image_url(data.get("primaryImage"))
    if asset_url is None:
        return None
    artist = _text(data.get("artistDisplayName"))
    title = _text(data.get("title")) or "Untitled"
    source_url = f"https://www.metmuseum.org/art/collection/search/{item_id}"
    result: dict[str, object] = {
        "item_id": item_id,
        "title": title,
        "artist": artist,
        "credit_line": _text(data.get("creditLine")),
        "attribution": f"{artist + ', ' if artist else ''}{title} — {_SOURCE}",
        "license": "CC0-1.0",
        "license_url": _LICENSE_URL,
        "rights": "Public domain (Met Open Access)",
        "rights_url": _RIGHTS_URL,
        "source": _SOURCE,
        "source_url": source_url,
    }
    thumbnail = _image_url(data.get("primaryImageSmall"))
    if thumbnail is not None:
        result["thumbnail_url"] = thumbnail
    return result, asset_url


async def _search(
    client: httpx.AsyncClient, query: str, limit: int
) -> tuple[dict[str, bytes], dict[str, object]]:
    try:
        async with asyncio.timeout(30):
            reply = await _get_json(client, "/search", params={"q": query, "hasImages": "true"})
            ids = reply.get("objectIDs")
            if ids is None and reply.get("total") == 0:
                ids = []
            if not isinstance(ids, list):
                raise WorkbenchError("The Met returned invalid collection search results.")
            results: list[dict[str, object]] = []
            seen: set[int] = set()
            for candidate in ids[:_SEARCH_CANDIDATES]:
                if not _item_id(candidate) or candidate in seen:
                    continue
                seen.add(candidate)
                item = await _eligible_item(client, candidate)
                if item is not None:
                    results.append(item[0])
                    if len(results) == limit:
                        break
            return {}, {
                "source": _SOURCE,
                "results": results,
                "candidate_count": len(ids),
                "checked_count": len(seen),
            }
    except TimeoutError:
        pass
    raise WorkbenchError("The Met collection search timed out.")


async def _download(
    client: httpx.AsyncClient, item_id: int
) -> tuple[dict[str, bytes], dict[str, object]]:
    item = await _eligible_item(client, item_id)
    if item is None:
        raise WorkbenchError("This Met item has no eligible public-domain image to download.")
    attribution, asset_url = item
    asset = await fetch_bytes(
        client,
        client.build_request(
            "GET", asset_url, headers={"Accept": "image/jpeg,image/png,image/webp"}
        ),
        service=_SOURCE,
        max_bytes=_MAX_ASSET_BYTES,
        content_types=_IMAGE_TYPES,
        deadline=25,
    )
    extension, mime = raster_type(asset, "Met asset")
    filename = f"met-{item_id}.{extension}"
    attribution_file = f"met-{item_id}-attribution.json"
    attribution.update(asset_url=asset_url, filename=filename, media_type=mime)
    sidecar = (json.dumps(attribution, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    return {filename: asset, attribution_file: sidecar}, {
        **attribution,
        "attribution_file": attribution_file,
    }


async def media(arguments: Mapping[str, object]) -> tuple[dict[str, bytes], dict[str, object]]:
    """Return search metadata or a freshly verified image plus an attribution sidecar."""
    action = arguments.get("action")
    if action == "search":
        if arguments.keys() - {"action", "query", "limit"}:
            raise WorkbenchError("Search accepts only a query and an optional limit.")
        query, limit = arguments.get("query"), arguments.get("limit", 5)
        if not isinstance(query, str) or not 1 <= len(query) <= _MAX_QUERY or not query.strip():
            raise WorkbenchError(
                f"Search requires a nonempty query of at most {_MAX_QUERY} characters."
            )
        if not all(char.isprintable() for char in query):
            raise WorkbenchError("Search query must not contain control characters.")
        if type(limit) is not int or not 1 <= limit <= _MAX_LIMIT:
            raise WorkbenchError(f"Search limit must be an integer from 1 to {_MAX_LIMIT}.")
        async with httpx.AsyncClient(
            trust_env=False, headers={"Accept": "application/json", "Accept-Encoding": "identity"}
        ) as client:
            return await _search(client, query.strip(), limit)
    if action == "download":
        item_id = arguments.get("item_id")
        if arguments.keys() - {"action", "item_id"} or not _item_id(item_id):
            raise WorkbenchError("Download accepts only a positive integer Met item_id.")
        async with httpx.AsyncClient(
            trust_env=False, headers={"Accept": "application/json", "Accept-Encoding": "identity"}
        ) as client:
            return await _download(client, item_id)
    raise WorkbenchError("Media action must be search or download.")
