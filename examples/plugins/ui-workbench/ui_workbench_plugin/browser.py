"""Bounded W3C WebDriver capture for trusted local pages, not a browser sandbox."""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import re
import struct
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import SplitResult, unquote, urlsplit

import httpx

from .files import WorkbenchError, workspace_file

_VIEWPORTS = {"desktop": (1440, 900), "mobile": (390, 844)}
_BROWSERS = {
    "chrome": ("chrome", "goog:chromeOptions"),
    "edge": ("MicrosoftEdge", "ms:edgeOptions"),
}
_MAX_STATES = 3
_MAX_STEPS = 8
_MAX_WAIT = 2000
_MAX_SELECTOR = 256
_MAX_TEXT = 1024
_STATE_PATTERN = r"^[a-z][a-z0-9_-]{0,31}$"
_DRIVER_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}")
_ELEMENT_KEY = "element-6066-11e4-a52e-4f735466cecf"
_CAPTURE_TIMEOUT = 60.0
_START_TIMEOUT = 12.0
_COMMAND_TIMEOUT = 8.0
_CLOSE_TIMEOUT = 3.0
_CONTROL_BYTES = 64 * 1024
_SCREENSHOT_BYTES = 8 * 1024 * 1024


def _object_schema(properties: dict, *required: str) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


_SELECTOR_SCHEMA = {"type": "string", "minLength": 1, "maxLength": _MAX_SELECTOR}
_STEPS_SCHEMA = {
    "type": "array",
    "maxItems": _MAX_STEPS,
    "items": {
        "oneOf": [
            _object_schema(
                {"action": {"const": "click"}, "selector": _SELECTOR_SCHEMA}, "action", "selector"
            ),
            _object_schema(
                {
                    "action": {"const": "type"},
                    "selector": _SELECTOR_SCHEMA,
                    "text": {"type": "string", "maxLength": _MAX_TEXT},
                },
                "action",
                "selector",
                "text",
            ),
            _object_schema(
                {
                    "action": {"const": "wait"},
                    "ms": {"type": "integer", "minimum": 0, "maximum": _MAX_WAIT},
                },
                "action",
                "ms",
            ),
        ],
    },
}
CAPTURE_PARAMETERS = _object_schema(
    {
        "target": {
            "type": "string",
            "minLength": 1,
            "maxLength": 1024,
            "description": (
                "Workspace-relative .html/.htm or a loopback HTTP(S) dev-server URL. "
                "Use trusted pages only."
            ),
        },
        "viewports": {
            "type": "array",
            "items": {"enum": list(_VIEWPORTS)},
            "minItems": 1,
            "maxItems": len(_VIEWPORTS),
            "uniqueItems": True,
            "description": "Defaults to desktop and mobile.",
        },
        "steps": {
            **_STEPS_SCHEMA,
            "description": "Shared steps; type replaces a text field value.",
        },
        "states": {
            "type": "array",
            "minItems": 1,
            "maxItems": _MAX_STATES,
            "description": "Named variants run after shared steps, each with a fresh profile.",
            "items": _object_schema(
                {"name": {"type": "string", "pattern": _STATE_PATTERN}, "steps": _STEPS_SCHEMA},
                "name",
            ),
        },
    },
    "target",
)

# Pass selectors and text as data. Do not use WebDriver's Element Send Keys: for a
# file input that command interprets the supplied text as a local path to upload.
_TYPE_SCRIPT = """
const element = document.querySelector(arguments[0]);
const textarea = element instanceof HTMLTextAreaElement;
const textInput = element instanceof HTMLInputElement &&
  ['text', 'search', 'email', 'url', 'tel', 'password', 'number'].includes(element.type);
if ((!textarea && !textInput) || element.matches(':disabled') || element.readOnly ||
    !element.getClientRects().length) return false;
element.focus();
if (!element.isConnected || document.activeElement !== element ||
    (!textarea && !['text', 'search', 'email', 'url', 'tel', 'password', 'number']
      .includes(element.type))) return false;
const prototype = textarea ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
Object.getOwnPropertyDescriptor(prototype, 'value').set.call(element, arguments[1]);
element.dispatchEvent(new Event('input', {bubbles: true, composed: true}));
element.dispatchEvent(new Event('change', {bubbles: true}));
return true;
"""

_DIAGNOSTICS_SCRIPT = """
const limit = 2000;
const root = document.documentElement;
const layoutWidth = root.clientWidth;
const layoutHeight = root.clientHeight;
const horizontalOverflowPx = Math.max(0, root.scrollWidth - layoutWidth);
const images = document.images;
let brokenImages = 0, pendingImages = 0;
for (let i = 0; i < Math.min(limit, images.length); i++) {
  const image = images[i];
  if (!(image.currentSrc || image.getAttribute('src') || image.getAttribute('srcset'))) continue;
  if (!image.complete) pendingImages++;
  else if (!image.naturalWidth) brokenImages++;
}
const rgb = value => {
  const match = /^rgb\\((\\d+), (\\d+), (\\d+)\\)$/.exec(value);
  return match ? match.slice(1).map(Number) : null;
};
const luminance = color => color.map(value => {
  value /= 255;
  return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
}).reduce((sum, value, index) => sum + value * [0.2126, 0.7152, 0.0722][index], 0);
const inspected = new Set();
let lowContrast = 0, contrastChecked = 0, walked = 0;
const walker = document.createTreeWalker(document.body || root, NodeFilter.SHOW_ALL);
let node;
while (walked < limit && (node = walker.nextNode())) {
  walked++;
  if (node.nodeType !== Node.TEXT_NODE || !node.data.slice(0, 1024).trim()) continue;
  const element = node.parentElement;
  if (!element || inspected.has(element)) continue;
  inspected.add(element);
  const style = getComputedStyle(element);
  if (style.visibility !== 'visible' || !element.getClientRects().length) continue;
  const foreground = rgb(style.color);
  if (!foreground) continue;
  let background = null;
  let current = element, depth = 0;
  for (; current && depth < 20; current = current.parentElement, depth++) {
    const layer = getComputedStyle(current);
    if (layer.opacity !== '1' || layer.filter !== 'none' || layer.mixBlendMode !== 'normal') break;
    if (background) continue;
    if (layer.backgroundImage !== 'none') break;
    if (layer.backgroundColor === 'rgba(0, 0, 0, 0)') continue;
    background = rgb(layer.backgroundColor);
    if (!background) break;
  }
  if (current) continue;
  const a = luminance(foreground), b = luminance(background || [255, 255, 255]);
  const size = parseFloat(style.fontSize);
  const large = size >= 24 || (size >= 18.67 && Number(style.fontWeight) >= 700);
  contrastChecked++;
  if ((Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05) < (large ? 3 : 4.5)) lowContrast++;
}
return {
  layout_width: layoutWidth, layout_height: layoutHeight,
  horizontal_overflow_px: horizontalOverflowPx,
  broken_images: brokenImages, pending_images: pendingImages,
  low_contrast_text_elements: lowContrast, contrast_checked_elements: contrastChecked,
  scan_limited: images.length > limit || walked === limit
};
"""


def _text(value: object, maximum: int, *, optional_empty: bool = False) -> bool:
    return (
        isinstance(value, str)
        and len(value) <= maximum
        and (optional_empty or bool(value.strip()))
        and not any(0xD800 <= ord(char) <= 0xDFFF or char == "\x00" for char in value)
    )


def _steps(items: object) -> list[dict]:
    if not isinstance(items, list) or len(items) > _MAX_STEPS:
        raise WorkbenchError("Capture steps must be a bounded list.")
    for item in items:
        if not isinstance(item, dict):
            raise WorkbenchError("Each capture step must be an object.")
        action = item.get("action")
        if action == "wait":
            ms = item.get("ms")
            valid = set(item) == {"action", "ms"} and type(ms) is int and 0 <= ms <= _MAX_WAIT
        elif action in ("click", "type"):
            valid = _text(item.get("selector"), _MAX_SELECTOR)
            if action == "click":
                valid = valid and set(item) == {"action", "selector"}
            else:
                valid = (
                    valid
                    and set(item) == {"action", "selector", "text"}
                    and _text(item.get("text"), _MAX_TEXT, optional_empty=True)
                )
        else:
            valid = False
        if not valid:
            raise WorkbenchError("Invalid capture step; use click, type, or wait with its fields.")
    return items


def _states(arguments: Mapping[str, object]) -> list[tuple[str, list[dict]]]:
    common = _steps(arguments.get("steps", []))
    if "states" not in arguments:
        return [("default", common)]
    raw = arguments["states"]
    if not isinstance(raw, list) or not 1 <= len(raw) <= _MAX_STATES:
        raise WorkbenchError("Capture states must be a nonempty bounded list.")
    states = []
    seen = set()
    for state in raw:
        if not isinstance(state, dict) or state.keys() - {"name", "steps"}:
            raise WorkbenchError("Each capture state must contain a name and optional steps.")
        name = state.get("name")
        if not isinstance(name, str) or not re.fullmatch(_STATE_PATTERN, name) or name in seen:
            raise WorkbenchError("Capture state names must be unique lowercase filename labels.")
        seen.add(name)
        states.append((name, common + _steps(state.get("steps", []))))
    return states


def _loopback_url(value: str) -> tuple[SplitResult, str]:
    if any(ord(char) <= 32 or ord(char) == 127 or char == "\\" for char in value):
        raise ValueError
    url = urlsplit(value)
    if url.scheme not in {"http", "https"} or not re.fullmatch(
        r"(?:[a-zA-Z0-9.]+|\[[0-9a-fA-F:]+\])(?::[0-9]{1,5})?", url.netloc
    ):
        raise ValueError
    host = url.hostname or ""
    if host != "localhost":
        address = ipaddress.ip_address(host)
        if not address.is_loopback:
            raise ValueError
        host = f"[{address}]" if address.version == 6 else str(address)
    port = url.port
    if port == 0:
        raise ValueError
    default_port = 443 if url.scheme == "https" else 80
    authority = host if port in (None, default_port) else f"{host}:{port}"
    return url, f"{url.scheme}://{authority}"


def _endpoint(value: str) -> str:
    try:
        if not isinstance(value, str) or "?" in value or "#" in value:
            raise ValueError
        url, _ = _loopback_url(value)
        if not url.port or not re.fullmatch(r"(/[a-zA-Z0-9_-]+)*/?", url.path):
            raise ValueError
    except ValueError:
        raise WorkbenchError(
            "Configure a loopback HTTP(S) WebDriver endpoint with a port."
        ) from None
    return value.rstrip("/")


@dataclass(frozen=True, slots=True)
class _Target:
    uri: str
    provenance: dict[str, str]
    origin: str | None = None


def _target(workspace: Path, value: object) -> _Target:
    if not isinstance(value, str) or not _text(value, 1024):
        raise WorkbenchError("Capture requires workspace-relative HTML or a loopback URL.")
    if value.lower().startswith(("http:", "https:")):
        try:
            url, origin = _loopback_url(value)
        except ValueError:
            raise WorkbenchError("Use a loopback HTTP(S) app URL without credentials.") from None
        return _Target(value, {"kind": "loopback_http", "url": origin + (url.path or "/")}, origin)
    source = workspace_file(workspace, value)
    if source.suffix.lower() not in {".html", ".htm"}:
        raise WorkbenchError("Capture requires an existing HTML file in the workspace.")
    return _Target(
        source.as_uri(),
        {"kind": "workspace_html", "path": source.relative_to(workspace.resolve()).as_posix()},
    )


def _capabilities(browser: str, viewport: str) -> dict:
    name, key = _BROWSERS[browser]
    width, height = _VIEWPORTS[viewport]
    mobile = viewport == "mobile"
    metrics: dict[str, object] = {
        "deviceMetrics": {
            "width": width,
            "height": height,
            "pixelRatio": 1,
            "mobile": mobile,
            "touch": mobile,
        },
    }
    if mobile:
        metrics["clientHints"] = {"platform": "Android", "mobile": True}
    options = {
        "args": ["--headless=new", f"--window-size={width},{height}", "--no-first-run"],
        "prefs": {"download_restrictions": 3, "credentials_enable_service": False},
        "mobileEmulation": metrics,
    }
    return {
        "capabilities": {
            "alwaysMatch": {
                "browserName": name,
                "pageLoadStrategy": "normal",
                "timeouts": {"implicit": 0, "pageLoad": 5000, "script": 3000},
                "unhandledPromptBehavior": "dismiss and notify",
                key: options,
            }
        },
    }


async def _command(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    label: str,
    payload: dict | None = None,
    *,
    timeout: float = _COMMAND_TIMEOUT,
    limit: int = _CONTROL_BYTES,
) -> object:
    try:
        async with asyncio.timeout(timeout):
            async with client.stream(method, path, json=payload) as response:
                if not response.is_success:
                    raise WorkbenchError(f"WebDriver could not {label}.")
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise WorkbenchError("WebDriver returned an unsupported response encoding.")
                claimed = response.headers.get("content-length")
                if claimed is not None and int(claimed) > limit:
                    raise WorkbenchError("WebDriver response exceeds the capture limit.")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > limit:
                        raise WorkbenchError("WebDriver response exceeds the capture limit.")
                    body.extend(chunk)
    except WorkbenchError:
        raise
    except httpx.HTTPError, TimeoutError, ValueError:
        raise WorkbenchError(f"WebDriver could not {label}; check the local driver.") from None
    if method == "DELETE" and not body:
        return None
    try:
        result = json.loads(body)
    except ValueError, RecursionError:
        raise WorkbenchError("WebDriver returned malformed JSON.") from None
    if not isinstance(result, dict) or "value" not in result:
        raise WorkbenchError("WebDriver returned a malformed protocol response.")
    value = result["value"]
    if isinstance(value, dict) and "error" in value:
        raise WorkbenchError(f"WebDriver could not {label}.")
    return value


async def _open_session(client: httpx.AsyncClient, browser: str, viewport: str) -> str:
    created = await _command(
        client,
        "POST",
        "session",
        "start a fresh browser session",
        _capabilities(browser, viewport),
        timeout=_START_TIMEOUT,
    )
    session_id = created.get("sessionId") if isinstance(created, dict) else None
    if not isinstance(session_id, str) or not _DRIVER_ID.fullmatch(session_id):
        raise WorkbenchError("WebDriver did not return a valid session ID.")
    return f"session/{session_id}"


async def _close_session(client: httpx.AsyncClient, opening: asyncio.Task[str]) -> bool:
    try:
        session = await opening
    except WorkbenchError:
        return True
    try:
        await _command(client, "DELETE", session, "close the browser", timeout=_CLOSE_TIMEOUT)
    except WorkbenchError:
        return False
    return True


@asynccontextmanager
async def _session(client: httpx.AsyncClient, browser: str, viewport: str) -> AsyncIterator[str]:
    # If cancellation races the POST, let the bounded request resolve so we can delete its ID.
    opening = asyncio.create_task(_open_session(client, browser, viewport))
    completed = False
    try:
        yield await asyncio.shield(opening)
        completed = True
    finally:
        closing = asyncio.create_task(_close_session(client, opening))
        try:
            closed = await asyncio.shield(closing)
        except asyncio.CancelledError:
            while not closing.done():
                try:
                    await asyncio.shield(closing)
                except asyncio.CancelledError:
                    continue
            raise
        if completed and not closed:
            raise WorkbenchError("WebDriver did not confirm that the browser session closed.")


async def _check_page(client: httpx.AsyncClient, session: str, target: _Target) -> None:
    value = await _command(client, "GET", f"{session}/url", "check the current page")
    try:
        if isinstance(value, str):
            if target.origin is not None:
                _, origin = _loopback_url(value)
                if origin == target.origin:
                    return
            else:
                actual, expected = urlsplit(value), urlsplit(target.uri)
                if (actual.scheme, actual.netloc) == (expected.scheme, expected.netloc) and unquote(
                    actual.path
                ) == unquote(expected.path):
                    return
    except ValueError:
        pass
    raise WorkbenchError(
        "The browser navigated away from the requested workspace HTML or app origin."
    )


async def _run_steps(
    client: httpx.AsyncClient, session: str, target: _Target, steps: list[dict]
) -> None:
    for step in steps:
        action = step["action"]
        if action == "wait":
            await asyncio.sleep(step["ms"] / 1000)
        elif action == "click":
            found = await _command(
                client,
                "POST",
                f"{session}/element",
                "find the click target",
                {"using": "css selector", "value": step["selector"]},
            )
            element = found.get(_ELEMENT_KEY) if isinstance(found, dict) else None
            if not isinstance(element, str) or not _DRIVER_ID.fullmatch(element):
                raise WorkbenchError("WebDriver did not return a valid click target.")
            await _command(client, "POST", f"{session}/element/{element}/click", "click", {})
        else:
            typed = await _command(
                client,
                "POST",
                f"{session}/execute/sync",
                "type into the field",
                {"script": _TYPE_SCRIPT, "args": [step["selector"], step["text"]]},
            )
            if typed is not True:
                raise WorkbenchError("Type needs an editable, visible text field or textarea.")
        await _check_page(client, session, target)


def _diagnostics(value: object) -> dict[str, object]:
    fields = {
        "layout_width": (1, 10_000_000),
        "layout_height": (1, 10_000_000),
        "horizontal_overflow_px": (0, 10_000_000),
        "broken_images": (0, 2000),
        "pending_images": (0, 2000),
        "low_contrast_text_elements": (0, 2000),
        "contrast_checked_elements": (0, 2000),
    }
    if not isinstance(value, dict) or type(value.get("scan_limited")) is not bool:
        raise WorkbenchError("WebDriver returned malformed page diagnostics.")
    result: dict[str, object] = {"scan_limited": value["scan_limited"]}
    for key, (minimum, maximum) in fields.items():
        number = value.get(key)
        if type(number) is not int or not minimum <= number <= maximum:
            raise WorkbenchError("WebDriver returned malformed page diagnostics.")
        result[key] = number
    return result


def _png(encoded: object) -> tuple[bytes, int, int]:
    if isinstance(encoded, str):
        try:
            image = base64.b64decode(encoded, validate=True)
        except ValueError:
            image = b""
        if (
            len(image) >= 45
            and image.startswith(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
            and image.endswith(b"\x00\x00\x00\x00IEND\xaeB`\x82")
        ):
            width, height = struct.unpack_from(">II", image, 16)
            if 0 < width <= 4096 and 0 < height <= 4096:
                return image, width, height
    raise WorkbenchError("WebDriver did not return a bounded PNG screenshot.")


async def capture(
    workspace: Path,
    arguments: Mapping[str, object],
    *,
    endpoint: str,
    browser: str,
) -> tuple[dict[str, bytes], dict[str, object]]:
    """Capture trusted workspace HTML or a loopback app; caller gates concurrency.

    Sessions use driver-created temporary profiles, one per state and viewport. Browser
    subresource requests/page scripts are not isolated by this adapter. Type replaces
    a text control's value and sends input/change events, not simulated keystrokes.
    """
    driver = _endpoint(endpoint)
    if browser not in _BROWSERS:
        raise WorkbenchError("Configure the browser as chrome or edge.")
    if arguments.keys() - CAPTURE_PARAMETERS["properties"].keys():
        raise WorkbenchError("Capture received unsupported arguments.")
    target = _target(workspace, arguments.get("target"))
    viewports = arguments.get("viewports", list(_VIEWPORTS))
    if (
        not isinstance(viewports, list)
        or not 1 <= len(viewports) <= len(_VIEWPORTS)
        or any(
            not isinstance(viewport, str) or viewport not in _VIEWPORTS for viewport in viewports
        )
        or len(set(viewports)) != len(viewports)
    ):
        raise WorkbenchError("Capture viewports must be unique desktop or mobile labels.")
    states = _states(arguments)
    images: dict[str, bytes] = {}
    records: list[dict[str, object]] = []
    try:
        async with (
            httpx.AsyncClient(
                base_url=driver + "/",
                trust_env=False,
                follow_redirects=False,
                headers={"Accept-Encoding": "identity"},
                timeout=_COMMAND_TIMEOUT,
            ) as client,
            asyncio.timeout(_CAPTURE_TIMEOUT),
        ):
            for state, steps in states:
                for viewport in viewports:
                    async with _session(client, browser, viewport) as session:
                        await _command(
                            client, "POST", f"{session}/url", "load the page", {"url": target.uri}
                        )
                        await _check_page(client, session, target)
                        await _run_steps(client, session, target, steps)
                        report = _diagnostics(
                            await _command(
                                client,
                                "POST",
                                f"{session}/execute/sync",
                                "inspect the page",
                                {"script": _DIAGNOSTICS_SCRIPT, "args": []},
                            )
                        )
                        await _check_page(client, session, target)
                        screenshot, width, height = _png(
                            await _command(
                                client,
                                "GET",
                                f"{session}/screenshot",
                                "capture the page",
                                limit=_SCREENSHOT_BYTES,
                            )
                        )
                        await _check_page(client, session, target)
                    filename = f"{state}-{viewport}.png"
                    images[filename] = screenshot
                    records.append(
                        {
                            "filename": filename,
                            "state": state,
                            "viewport": viewport,
                            "screenshot_width": width,
                            "screenshot_height": height,
                            "diagnostics": report,
                        }
                    )
    except TimeoutError:
        raise WorkbenchError("The browser capture reached its time limit.") from None
    return images, {
        "source": target.provenance,
        "browser": browser,
        "captures": records,
    }
