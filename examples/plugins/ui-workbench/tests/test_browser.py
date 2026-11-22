import asyncio
import base64
import json

import httpx
import pytest
from ui_workbench_plugin import browser as adapter
from ui_workbench_plugin.files import WorkbenchError

_ENDPOINT = "http://127.0.0.1:9515/wd/hub"
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jNicAAAAASUVORK5CYII="
)
_DIAGNOSTICS = {
    "layout_width": 390,
    "layout_height": 844,
    "horizontal_overflow_px": 48,
    "broken_images": 2,
    "pending_images": 0,
    "low_contrast_text_elements": 1,
    "contrast_checked_elements": 3,
    "scan_limited": False,
}


def _value(value):
    return httpx.Response(200, json={"value": value})


class _Driver:
    def __init__(self):
        self.sessions = 0
        self.live = {}
        self.calls = []
        self.override = None

    async def __call__(self, request):
        assert request.url.host == "127.0.0.1"
        assert request.url.port == 9515
        assert request.headers["accept-encoding"] == "identity"
        method = request.method
        path = request.url.path.removeprefix("/wd/hub/")
        data = json.loads(request.content) if request.content else None
        self.calls.append((method, path, data))
        if self.override is not None:
            response = await self.override(method, path, data)
            if response is not None:
                return response
        if (method, path) == ("POST", "session"):
            self.sessions += 1
            session = f"session-{self.sessions}"
            self.live[session] = "about:blank"
            return _value({"sessionId": session, "capabilities": {}})
        _, session, *command = path.split("/")
        assert session in self.live
        match method, command:
            case "DELETE", []:
                del self.live[session]
                return _value(None)
            case "POST", ["url"]:
                self.live[session] = data["url"]
                return _value(None)
            case "GET", ["url"]:
                return _value(self.live[session] + "#in-page-state")
            case "POST", ["element"]:
                return _value({adapter._ELEMENT_KEY: "found-1"})
            case "POST", ["element", "found-1", "click"]:
                return _value(None)
            case "POST", ["execute", "sync"]:
                if data["script"] == adapter._TYPE_SCRIPT:
                    return _value(True)
                assert data == {"script": adapter._DIAGNOSTICS_SCRIPT, "args": []}
                return _value({**_DIAGNOSTICS, "text": "must not appear in metadata"})
            case "GET", ["screenshot"]:
                return _value(base64.b64encode(_PNG).decode())
        pytest.fail(f"Unexpected WebDriver command: {method} {path}")


@pytest.fixture
async def driver(monkeypatch):
    fake = _Driver()
    real_client = httpx.AsyncClient

    def client(**kwargs):
        assert not kwargs["trust_env"]
        assert not kwargs["follow_redirects"]
        return real_client(transport=httpx.MockTransport(fake), **kwargs)

    monkeypatch.setattr(adapter.httpx, "AsyncClient", client)
    return fake


@pytest.fixture
def page(tmp_path):
    path = tmp_path / "Page #1.html"
    path.write_text("<!doctype html><title>Workbench fixture</title>", encoding="utf-8")
    return path


async def _capture(page, arguments=None, *, endpoint=_ENDPOINT, browser="chrome"):
    requested = {"target": page.name, "viewports": ["desktop"]}
    if arguments is not None:
        requested.update(arguments)
    return await adapter.capture(page.parent, requested, endpoint=endpoint, browser=browser)


async def test_state_viewport_matrix_uses_fresh_sessions_and_fixed_commands(driver, page):
    text = "new value with 'quotes' and <tags>"
    images, metadata = await _capture(
        page,
        {
            "viewports": ["desktop", "mobile"],
            "steps": [{"action": "click", "selector": "#open"}],
            "states": [
                {"name": "initial"},
                {
                    "name": "edited",
                    "steps": [
                        {"action": "type", "selector": "#name", "text": text},
                        {"action": "wait", "ms": 0},
                    ],
                },
            ],
        },
        browser="edge",
    )
    assert list(images) == [
        "initial-desktop.png",
        "initial-mobile.png",
        "edited-desktop.png",
        "edited-mobile.png",
    ]
    assert set(images.values()) == {_PNG}
    assert metadata["source"] == {"kind": "workspace_html", "path": page.name}
    assert metadata["browser"] == "edge"
    assert metadata["captures"][-1] == {
        "state": "edited",
        "viewport": "mobile",
        "filename": "edited-mobile.png",
        "screenshot_width": 1,
        "screenshot_height": 1,
        "diagnostics": _DIAGNOSTICS,
    }
    assert driver.sessions == 4
    assert not driver.live
    cursor = 0
    for session in range(1, 5):
        prefix = f"session/session-{session}"
        start = driver.calls[cursor]
        assert start[:2] == ("POST", "session")
        capabilities = start[2]["capabilities"]["alwaysMatch"]
        assert capabilities["browserName"] == "MicrosoftEdge"
        options = capabilities["ms:edgeOptions"]
        assert "--headless=new" in options["args"]
        assert not any("user-data-dir" in arg for arg in options["args"])
        mobile = session % 2 == 0
        assert options["mobileEmulation"]["deviceMetrics"] == {
            "width": 390 if mobile else 1440,
            "height": 844 if mobile else 900,
            "pixelRatio": 1,
            "mobile": mobile,
            "touch": mobile,
        }
        close = driver.calls.index(("DELETE", prefix, None))
        commands = driver.calls[cursor + 1 : close]
        assert all(path.startswith(prefix + "/") for _, path, _ in commands)
        assert commands[0] == ("POST", f"{prefix}/url", {"url": page.as_uri()})
        assert (
            "POST",
            f"{prefix}/element",
            {"using": "css selector", "value": "#open"},
        ) in commands
        assert ("POST", f"{prefix}/element/found-1/click", {}) in commands
        typed = [
            data for _, _, data in commands if data and data.get("script") == adapter._TYPE_SCRIPT
        ]
        assert typed == (
            [{"script": adapter._TYPE_SCRIPT, "args": ["#name", text]}] if session > 2 else []
        )
        assert commands[-2:] == [
            ("GET", f"{prefix}/screenshot", None),
            ("GET", f"{prefix}/url", None),
        ]
        cursor = close + 1
    assert cursor == len(driver.calls)


async def test_plain_capture_defaults_to_both_viewports(driver, page):
    images, _ = await adapter.capture(
        page.parent,
        {"target": page.name},
        endpoint=_ENDPOINT,
        browser="chrome",
    )
    assert list(images) == ["default-desktop.png", "default-mobile.png"]
    assert "goog:chromeOptions" in driver.calls[0][2]["capabilities"]["alwaysMatch"]
    assert not driver.live


@pytest.mark.parametrize(
    "arguments",
    [
        {"target": "../outside.html"},
        {"target": "https://example.invalid/"},
        {"target": "not-html.txt"},
        {"viewports": ["desktop", "desktop"]},
        {"viewports": [{}]},
        {"states": [{"name": "../outside"}]},
        {"states": [{"name": "a"}, {"name": "a"}]},
        {"steps": [{"action": "wait", "ms": True}]},
        {"steps": [{"action": "wait", "ms": 2001}]},
        {"steps": [{"action": "wait", "ms": 0}] * 9},
        {"steps": [{"action": "type", "selector": "#x", "text": "x" * 1025}]},
        {"steps": [{"action": "click", "selector": "#x", "script": "throw 1"}]},
        {"steps": [{"action": "execute", "text": "throw 1"}]},
        {"endpoint": "http://127.0.0.1:9515"},
    ],
)
async def test_rejects_invalid_arguments_before_opening(driver, page, arguments):
    (page.parent / "not-html.txt").write_text("not HTML", encoding="utf-8")
    with pytest.raises(WorkbenchError):
        await _capture(page, arguments)
    assert not driver.calls


@pytest.mark.parametrize(
    "endpoint",
    [
        "",
        "http://example.invalid:9515",
        "http://localhost",
        "http://localhost:9515/?debug=1",
        "http://user@localhost:9515",
        "http://0.0.0.0:9515",
        "http://127.0.0.1:9515/../",
    ],
)
async def test_rejects_other_driver_endpoints(driver, page, endpoint):
    with pytest.raises(WorkbenchError, match="loopback"):
        await _capture(page, endpoint=endpoint)
    assert not driver.calls


class _LargeStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"x" * 100
        yield b"y" * 100
        pytest.fail("Capture should stop consuming the response once it reaches the limit")


@pytest.mark.parametrize(
    ("fault", "message"),
    [
        ("driver", "could not capture"),
        ("redirect", "could not capture"),
        ("oversize", "exceeds the capture limit"),
        ("malformed", "malformed JSON"),
        ("encoding", "unsupported response encoding"),
        ("not-png", "PNG screenshot"),
        ("navigation", "navigated away"),
        ("diagnostics", "malformed page diagnostics"),
        ("cleanup", "did not confirm"),
    ],
)
async def test_driver_failure_is_safe_and_attempts_session_cleanup(
    driver,
    page,
    monkeypatch,
    fault,
    message,
):
    monkeypatch.setattr(adapter, "_SCREENSHOT_BYTES", 150)

    async def fail(method, path, data):
        if fault == "cleanup" and method == "DELETE":
            return httpx.Response(500)
        if fault == "navigation" and method == "GET" and path.endswith("/url"):
            return _value("https://example.invalid/?secret=secret-data")
        if fault == "diagnostics" and path.endswith("/execute/sync"):
            return _value({**_DIAGNOSTICS, "layout_width": True})
        if not path.endswith("/screenshot"):
            return None
        if fault == "driver":
            return httpx.Response(500, json={"value": {"message": "secret-data"}})
        if fault == "redirect":
            return httpx.Response(302, headers={"location": "https://example.invalid"})
        if fault == "oversize":
            return httpx.Response(200, stream=_LargeStream())
        if fault == "encoding":
            return httpx.Response(200, headers={"content-encoding": "gzip"}, stream=_LargeStream())
        if fault == "malformed":
            return httpx.Response(200, content=b"secret-data")
        if fault == "not-png":
            return _value(base64.b64encode(b"secret-data").decode())
        return None

    driver.override = fail
    with pytest.raises(WorkbenchError, match=message) as caught:
        await _capture(page)
    assert "secret-data" not in str(caught.value)
    assert driver.calls[-1] == ("DELETE", "session/session-1", None)
    if fault != "cleanup":
        assert not driver.live


@pytest.mark.parametrize("phase", ["opening", "capture", "closing"])
async def test_cancellation_closes_created_session(driver, page, phase):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def pause(method, path, _data):
        paused = (
            (phase == "opening" and path == "session")
            or (phase == "capture" and path.endswith("/screenshot"))
            or (phase == "closing" and method == "DELETE")
        )
        if paused:
            entered.set()
            await release.wait()
        return None

    driver.override = pause
    task = asyncio.create_task(_capture(page))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
    finally:
        release.set()
        if not task.done():
            task.cancel()
    assert driver.calls[-1] == ("DELETE", "session/session-1", None)
    assert not driver.live


async def test_overall_deadline_is_bounded_and_cleans_up(driver, page, monkeypatch):
    monkeypatch.setattr(adapter, "_CAPTURE_TIMEOUT", 0.02)

    async def pause(_method, path, _data):
        if path.endswith("/screenshot"):
            await asyncio.Event().wait()
        return None

    driver.override = pause
    with pytest.raises(WorkbenchError, match="capture reached its time limit"):
        await asyncio.wait_for(_capture(page), timeout=2)
    assert driver.calls[-1] == ("DELETE", "session/session-1", None)
    assert not driver.live


@pytest.mark.parametrize(
    ("target", "route", "source"),
    [
        (
            "http://LOCALHOST:5173/start?token=private#view",
            "http://localhost:5173/settings",
            "http://localhost:5173/start",
        ),
        (
            "https://[0:0:0:0:0:0:0:1]:443/?token=private",
            "https://[::1]/settings",
            "https://[::1]/",
        ),
    ],
)
async def test_loopback_app_allows_same_origin_routes(driver, page, target, route, source):
    async def route_click(method, path, _data):
        if method == "POST" and path.endswith("/click"):
            driver.live["session-1"] = route
        return None

    driver.override = route_click
    images, metadata = await _capture(
        page,
        {
            "target": target,
            "steps": [{"action": "click", "selector": "a"}],
        },
    )
    assert list(images) == ["default-desktop.png"]
    assert metadata["source"] == {"kind": "loopback_http", "url": source}
    assert "private" not in str(metadata)
    assert ("POST", "session/session-1/url", {"url": target}) in driver.calls
    assert not driver.live


@pytest.mark.parametrize(
    "target",
    [
        "http://localhost.example.invalid:5173/",
        "http://localhost@127.0.0.1:5173/",
        "http://127.0.0.1:5173\\@example.invalid/",
        "http://localhost:0/",
        "http://localhost:65536/",
        "http://192.168.1.1:5173/",
        "http://[::1]bad/",
        "http://127.1/",
        "http://2130706433/",
        "file:///example.html",
    ],
)
async def test_target_rejects_non_loopback_and_ambiguous_urls(driver, page, target):
    with pytest.raises(WorkbenchError):
        await _capture(page, {"target": target})
    assert not driver.calls


@pytest.mark.parametrize(
    ("phase", "redirect"),
    [
        ("load", "http://127.0.0.1:8080/other-port"),
        ("click", "https://127.0.0.1:5173/other-scheme"),
        ("click", "http://localhost:5173/other-host"),
        ("click", "https://example.invalid/secret"),
    ],
)
async def test_rejects_cross_origin_navigation_before_screenshot(driver, page, phase, redirect):
    async def navigate(method, path, _data):
        if phase == "load" and method == "GET" and path.endswith("/url"):
            return _value(redirect)
        if phase == "click" and method == "POST" and path.endswith("/click"):
            driver.live["session-1"] = redirect
        return None

    driver.override = navigate
    with pytest.raises(WorkbenchError, match="navigated away") as error:
        await _capture(
            page,
            {
                "target": "http://127.0.0.1:5173/",
                "steps": [{"action": "click", "selector": "a"}],
            },
        )
    assert redirect not in str(error.value)
    assert not any(path.endswith("/screenshot") for _, path, _ in driver.calls)
    assert driver.calls[-1] == ("DELETE", "session/session-1", None)
    assert not driver.live
