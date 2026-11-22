import base64
import json
from email import policy
from email.parser import BytesParser
from io import BytesIO

import httpx
import pytest
from PIL import Image
from ui_workbench_plugin.files import WorkbenchError
from ui_workbench_plugin.image import image

_SETTINGS = {
    "endpoint": "https://image.test/v1/",
    "model": "configured-model",
    "api_key": "private-test-token",
}
_GENERATE = {"action": "generate", "prompt": "A blue square"}


@pytest.fixture(autouse=True)
def mock_provider(monkeypatch):
    requests = []
    client = httpx.AsyncClient

    def handler(request):
        pytest.fail(f"Unexpected request: {request.url}")

    def record(request):
        requests.append(request)
        assert request.url.host == "image.test"
        assert request.headers["Authorization"] == "Bearer private-test-token"
        return handler(request)

    def mock_client(**kwargs):
        assert kwargs["trust_env"] is False
        return client(transport=httpx.MockTransport(record), **kwargs)

    def install(callback):
        nonlocal handler
        handler = callback
        return requests

    monkeypatch.setattr(httpx, "AsyncClient", mock_client)
    return install


@pytest.fixture
def png():
    stream = BytesIO()
    Image.new("RGB", (2, 2), "blue").save(stream, "PNG")
    return stream.getvalue()


def _image_reply(png):
    return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(png).decode()}]})


async def test_generate_posts_json_and_accepts_inline_image(mock_provider, png):
    def respond(request):
        assert request.method == "POST"
        assert str(request.url) == "https://image.test/v1/images/generations"
        assert json.loads(request.content) == {
            "model": "configured-model",
            "prompt": "A blue square",
            "n": 1,
        }
        assert request.headers["Accept-Encoding"] == "identity"
        return _image_reply(png)

    requests = mock_provider(respond)
    files, result = await image(None, _GENERATE, **_SETTINGS)
    assert len(requests) == 1
    assert files == {"generated.png": png}
    assert result == {"action": "generate", "filename": "generated.png", "media_type": "image/png"}


async def test_edit_posts_workspace_image_as_multipart(mock_provider, tmp_path, png):
    (tmp_path / "private-name.png").write_bytes(png)

    def respond(request):
        assert request.method == "POST"
        assert str(request.url) == "https://image.test/v1/images/edits"
        header = f"Content-Type: {request.headers['content-type']}\r\n\r\n".encode()
        message = BytesParser(policy=policy.default).parsebytes(header + request.content)
        parts = {
            part.get_param("name", header="content-disposition"): part
            for part in message.iter_parts()
        }
        assert set(parts) == {"model", "prompt", "image", "n", "size", "background"}
        assert parts["model"].get_payload(decode=True) == b"configured-model"
        assert parts["prompt"].get_payload(decode=True) == b"Make it green"
        assert parts["size"].get_payload(decode=True) == b"1536x1024"
        assert parts["background"].get_payload(decode=True) == b"transparent"
        assert parts["image"].get_filename() == "input.png"
        assert parts["image"].get_content_type() == "image/png"
        assert parts["image"].get_payload(decode=True) == png
        return _image_reply(png)

    mock_provider(respond)
    files, result = await image(
        tmp_path,
        {
            "action": "edit",
            "path": "private-name.png",
            "prompt": "Make it green",
            "size": "1536x1024",
            "background": "transparent",
        },
        **_SETTINGS,
    )
    assert files == {"edited.png": png}
    assert result["filename"] == "edited.png"


@pytest.mark.parametrize(("option", "value"), [("size", "10x10"), ("background", None)])
async def test_generate_rejects_unsupported_options(option, value):
    with pytest.raises(WorkbenchError, match=f"Image {option} must be one of"):
        await image(None, {**_GENERATE, option: value}, **_SETTINGS)


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://image.test/v1",
        "https://user:pass@image.test/v1",
        "https://image.test/v1?key=not-for-output",
        "https://image.test/v1#fragment",
        "https://image.test/v1/../custom",
    ],
)
async def test_invalid_endpoint_never_receives_secrets(endpoint):
    with pytest.raises(WorkbenchError, match="valid HTTPS") as exc:
        await image(None, _GENERATE, **{**_SETTINGS, "endpoint": endpoint})
    assert "not-for-output" not in str(exc.value)
    assert "private-test-token" not in str(exc.value)


async def test_edit_rejects_outside_and_invalid_files(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / "outside.png").write_bytes(b"private bytes")
    (workspace / "bad.png").write_bytes(b"not a PNG")
    edit = {"action": "edit", "prompt": "Make it green"}
    with pytest.raises(WorkbenchError, match="inside the current workspace"):
        await image(workspace, {**edit, "path": "../outside.png"}, **_SETTINGS)
    with pytest.raises(WorkbenchError, match="valid JPEG, PNG, or WebP"):
        await image(workspace, {**edit, "path": "bad.png"}, **_SETTINGS)


async def test_edit_rejects_oversized_upload(tmp_path, png, monkeypatch):
    monkeypatch.setattr("ui_workbench_plugin.image._MAX_INPUT_BYTES", len(png) - 1)
    (tmp_path / "input.png").write_bytes(png)
    with pytest.raises(WorkbenchError, match="byte size limit"):
        await image(
            tmp_path, {"action": "edit", "prompt": "Blue", "path": "input.png"}, **_SETTINGS
        )


@pytest.mark.parametrize(
    "entry,error",
    [
        ({"url": "https://image.test/not-requested.png"}, "not a URL"),
        ({"b64_json": "$not-base64$"}, "invalid base64"),
        ({"b64_json": "bm90IGFuIGltYWdl"}, "valid JPEG, PNG, or WebP"),
    ],
)
async def test_generate_rejects_invalid_image_results(mock_provider, entry, error):
    requests = mock_provider(lambda _: httpx.Response(200, json={"data": [entry]}))
    with pytest.raises(WorkbenchError, match=error):
        await image(None, _GENERATE, **_SETTINGS)
    assert len(requests) == 1


async def test_generate_rejects_oversized_decoded_image(mock_provider, png, monkeypatch):
    monkeypatch.setattr("ui_workbench_plugin.image._MAX_OUTPUT_BYTES", len(png) - 1)
    mock_provider(lambda _: _image_reply(png))
    with pytest.raises(WorkbenchError, match="output exceeds the image size limit"):
        await image(None, _GENERATE, **_SETTINGS)


async def test_generate_does_not_follow_redirect_or_expose_provider_error(mock_provider):
    requests = mock_provider(
        lambda _: httpx.Response(
            307,
            headers={"Location": "https://evil.test/redirect"},
            text="private-test-token",
        )
    )
    with pytest.raises(WorkbenchError, match="HTTP 307") as exc:
        await image(None, _GENERATE, **_SETTINGS)
    assert len(requests) == 1
    assert "private-test-token" not in str(exc.value)
    assert "evil.test" not in str(exc.value)


async def test_transport_error_discards_request_and_message(mock_provider):
    def respond(request):
        raise httpx.ReadTimeout("private-test-token", request=request)

    mock_provider(respond)
    with pytest.raises(WorkbenchError, match="failed or timed out") as exc:
        await image(None, _GENERATE, **_SETTINGS)
    assert "private-test-token" not in str(exc.value)
    assert exc.value.__cause__ is None
    assert exc.value.__context__ is None


async def test_generate_rejects_too_many_pixels(mock_provider, png, monkeypatch):
    monkeypatch.setattr("ui_workbench_plugin.http.MAX_PIXELS", 3)
    mock_provider(lambda _: _image_reply(png))
    with pytest.raises(WorkbenchError, match="pixel limit"):
        await image(None, _GENERATE, **_SETTINGS)


class _UnexpectedBody(httpx.AsyncByteStream):
    async def __aiter__(self):
        pytest.fail("Invalid response should be rejected before reading its body")
        yield b""


@pytest.mark.parametrize(
    "headers,error",
    [
        ({"Content-Type": "text/html"}, "unexpected content type"),
        ({"Content-Encoding": "gzip"}, "unsupported content encoding"),
        ({"Content-Length": "1000000000"}, "oversized response"),
    ],
)
async def test_response_headers_rejected_before_streaming(mock_provider, headers, error):
    mock_provider(
        lambda _: httpx.Response(
            200, headers={"Content-Type": "application/json", **headers}, stream=_UnexpectedBody()
        )
    )
    with pytest.raises(WorkbenchError, match=error):
        await image(None, _GENERATE, **_SETTINGS)
