import json
from io import BytesIO

import httpx
import pytest
from PIL import Image
from ui_workbench_plugin.files import WorkbenchError
from ui_workbench_plugin.media import media

_API_HOST = "collectionapi.metmuseum.org"
_ASSET = "https://images.metmuseum.org/CRDImages/ep/original/test.jpg"
_THUMB = "https://images.metmuseum.org/CRDImages/ep/web-large/test.jpg"


@pytest.fixture(autouse=True)
def mock_met(monkeypatch):
    requests = []
    client = httpx.AsyncClient

    def handler(request):
        pytest.fail(f"Unexpected request: {request.url}")

    def record(request):
        requests.append(request)
        assert "Authorization" not in request.headers
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


def _object(item_id=7, **overrides):
    return httpx.Response(
        200,
        json={
            "objectID": item_id,
            "isPublicDomain": True,
            "primaryImage": _ASSET,
            "primaryImageSmall": _THUMB,
            "title": "Study of light",
            "artistDisplayName": "Test artist",
            "creditLine": "Test gift",
            **overrides,
        },
    )


async def test_search_filters_rights_and_never_fetches_images(mock_met):
    def respond(request):
        assert request.method == "GET"
        assert request.url.host == _API_HOST
        if request.url.path.endswith("/search"):
            assert dict(request.url.params) == {"q": "light & color", "hasImages": "true"}
            return httpx.Response(200, json={"objectIDs": [1, 1, 2, 7, 8]})
        if request.url.path.endswith("/objects/1"):
            return _object(1, isPublicDomain=False)
        if request.url.path.endswith("/objects/2"):
            return _object(2, primaryImage="https://untrusted.example/test.jpg")
        assert request.url.path == "/public/collection/v1/objects/7"
        return _object()

    requests = mock_met(respond)
    files, result = await media({"action": "search", "query": "light & color", "limit": 1})
    assert files == {}
    assert result["checked_count"] == 3
    assert len(requests) == 4
    [item] = result["results"]
    assert item["item_id"] == 7
    assert item["title"] == "Study of light"
    assert item["license"] == "CC0-1.0"
    assert item["rights_url"].startswith("https://www.metmuseum.org/")
    assert item["thumbnail_url"] == _THUMB
    assert item["source_url"].endswith("/search/7")
    assert "asset_url" not in item


async def test_download_refetches_metadata_and_publishes_attribution(mock_met):
    stream = BytesIO()
    Image.new("RGB", (2, 2)).save(stream, "JPEG")
    pixels = stream.getvalue()

    def respond(request):
        if request.url.host == _API_HOST:
            assert request.url.path == "/public/collection/v1/objects/7"
            return _object(title="A painting\nwith a title")
        assert str(request.url) == _ASSET
        return httpx.Response(200, content=pixels, headers={"Content-Type": "image/jpeg"})

    requests = mock_met(respond)
    files, result = await media({"action": "download", "item_id": 7})
    assert [request.url.host for request in requests] == [_API_HOST, "images.metmuseum.org"]
    assert files["met-7.jpg"] == pixels
    sidecar = json.loads(files["met-7-attribution.json"])
    assert sidecar == {key: value for key, value in result.items() if key != "attribution_file"}
    assert sidecar["attribution"] == (
        "Test artist, A painting with a title — The Metropolitan Museum of Art"
    )
    assert sidecar["asset_url"] == _ASSET


@pytest.mark.parametrize(
    "override,error",
    [
        ({"isPublicDomain": 1}, "no eligible"),
        ({"objectID": 8}, "mismatched"),
        (
            {"primaryImage": "https://images.metmuseum.org.evil/CRDImages/ep/original/x.jpg"},
            "no eligible",
        ),
        (
            {"primaryImage": "https://images.metmuseum.org/CRDImages/ep/original/%2e%2e/x.jpg"},
            "no eligible",
        ),
        ({"primaryImage": "https://images.metmuseum.org/redirect.jpg?q=x"}, "no eligible"),
    ],
)
async def test_download_rejects_unverified_item_before_asset_request(mock_met, override, error):
    requests = mock_met(lambda _: _object(**override))
    with pytest.raises(WorkbenchError, match=error):
        await media({"action": "download", "item_id": 7})
    assert len(requests) == 1


@pytest.mark.parametrize("url", [_THUMB, "https://untrusted.example/redirect.jpg"])
async def test_download_does_not_follow_redirects(mock_met, url):
    def respond(request):
        if request.url.host == _API_HOST:
            return _object()
        return httpx.Response(302, headers={"Location": url})

    requests = mock_met(respond)
    with pytest.raises(WorkbenchError, match="HTTP 302"):
        await media({"action": "download", "item_id": 7})
    assert len(requests) == 2


async def test_rejects_arbitrary_download_url_and_boolean_id():
    with pytest.raises(WorkbenchError, match="item_id"):
        await media({"action": "download", "item_id": 7, "url": _ASSET})
    with pytest.raises(WorkbenchError, match="item_id"):
        await media({"action": "download", "item_id": True})


class _Chunks(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"x" * 64_000
        yield b"y" * 2_000


async def test_download_bounds_chunked_response_without_content_length(mock_met, monkeypatch):
    monkeypatch.setattr("ui_workbench_plugin.media._MAX_ASSET_BYTES", 65_000)

    def respond(request):
        if request.url.host == _API_HOST:
            return _object()
        return httpx.Response(200, headers={"Content-Type": "image/jpeg"}, stream=_Chunks())

    mock_met(respond)
    with pytest.raises(WorkbenchError, match="size limit"):
        await media({"action": "download", "item_id": 7})


async def test_search_caps_number_of_candidates(mock_met):
    def respond(request):
        if request.url.path.endswith("/search"):
            return httpx.Response(200, json={"objectIDs": list(range(1, 101))})
        return _object(int(request.url.path.rsplit("/", 1)[1]), isPublicDomain=False)

    requests = mock_met(respond)
    files, result = await media({"action": "search", "query": "light"})
    assert files == {}
    assert result["results"] == []
    assert result["candidate_count"] == 100
    assert result["checked_count"] == len(requests) - 1 == 20
