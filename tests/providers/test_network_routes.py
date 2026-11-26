from __future__ import annotations

import asyncio
import json
from contextlib import aclosing, contextmanager
from functools import partial
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import httpx
import pytest

from ouro.core.provider import ProviderRetryPolicy
from ouro.providers import OpenAIChatCompletionsProvider, http_transport
from ouro.providers.http_transport import HttpxProviderHTTPTransport
from ouro.providers.openai_responses.adapter import OpenAIResponsesProvider
from tests.providers.responses_fixtures import delta
from tests.providers.test_request_authentication import collect, response

PROXY = "http://proxy.test:8080"


@contextmanager
def local_endpoint(name):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"route": name}).encode())

        def log_message(self, *args):
            pass

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            thread.join()


async def test_live_proxy_selection_and_target_bypass(monkeypatch):
    with local_endpoint("origin") as origin, local_endpoint("proxy") as proxy:
        monkeypatch.setenv("http_proxy", proxy)
        monkeypatch.setenv("no_proxy", "unused.test")
        settings = {"https": proxy}
        monkeypatch.setattr(http_transport, "getproxies", settings.copy)

        async with aclosing(HttpxProviderHTTPTransport()) as transport:

            async def post():
                result = await transport.post_json(url=origin, headers={}, payload={}, timeout=3)
                return result["route"]

            assert await post() == "origin"
            settings["all"] = proxy.removeprefix("http://")
            assert await post() == "proxy"
            authority = httpx.URL(origin).netloc.decode()
            with monkeypatch.context() as system:
                system.setattr(http_transport, "proxy_bypass", lambda host: host == authority)
                assert await post() == "origin"
            assert await post() == "proxy"
            monkeypatch.setenv("no_proxy", authority)
            assert await post() == "origin"


def mock_routes(monkeypatch, handler):
    monkeypatch.setenv("https_proxy", PROXY)
    monkeypatch.setenv("no_proxy", "unused.test")
    monkeypatch.setattr(
        httpx, "AsyncHTTPTransport", lambda *, proxy: httpx.MockTransport(partial(handler, proxy))
    )


@pytest.mark.parametrize("streaming", [False, True])
async def test_stalled_proxy_recovers_and_reuses_working_route(monkeypatch, streaming):
    calls = []
    stalled = PROXY

    async def handle(proxy, request):
        calls.append(proxy)
        if proxy == stalled and request.url.host == "model.test":
            await asyncio.Event().wait()
        return response(OpenAIChatCompletionsProvider, streaming)

    mock_routes(monkeypatch, handle)
    async with aclosing(HttpxProviderHTTPTransport()) as transport:

        def provider(host):
            return OpenAIChatCompletionsProvider(
                base_url=f"https://{host}",
                transport=transport,
                timeout=0.03,
                retry_policy=ProviderRetryPolicy(max_attempts=2, initial_backoff_seconds=0),
            )

        model = provider("model.test")
        events = await collect(model, streaming)
        assert [e.type for e in events] == ["retry_scheduled", "text_delta", "done"]
        assert calls == [PROXY, None]
        calls.clear()

        assert (await collect(provider("other.test"), streaming))[-1].type == "done"
        assert (await collect(model, streaming))[-1].type == "done"
        assert calls == [PROXY, None]
        calls.clear()

        new_proxy = "http://other-proxy.test:8080"
        monkeypatch.setenv("https_proxy", new_proxy)
        assert (await collect(model, streaming))[-1].type == "done"
        assert calls == [new_proxy]
        calls.clear()

        stalled = new_proxy
        assert (await collect(model, streaming))[-1].type == "done"
        stalled = None
        assert (await collect(model, streaming))[-1].type == "done"
        assert calls == [new_proxy, None, None, new_proxy]


@pytest.mark.parametrize("attempts", [1, 2])
async def test_network_failures_share_one_budget_and_report_the_last_error(monkeypatch, attempts):
    calls = []

    def handle(proxy, request):
        calls.append(proxy)
        if proxy:
            raise httpx.ProxyError("private-proxy", request=request)
        raise httpx.ConnectTimeout("private-direct", request=request)

    mock_routes(monkeypatch, handle)
    async with aclosing(HttpxProviderHTTPTransport()) as transport:
        provider = OpenAIChatCompletionsProvider(
            base_url="https://model.test",
            transport=transport,
            retry_policy=ProviderRetryPolicy(max_attempts=attempts, initial_backoff_seconds=0),
        )
        events = await collect(provider)
    assert calls == [PROXY, None][:attempts]
    assert events[-1].error_code == (
        "provider_proxy_error" if attempts == 1 else "provider_connect_timeout"
    )
    assert "private-" not in repr(events)


async def test_http_status_retries_on_the_same_route(monkeypatch):
    calls = []

    def handle(proxy, request):
        calls.append(proxy)
        if len(calls) == 1:
            return httpx.Response(503)
        return response(OpenAIChatCompletionsProvider, True)

    mock_routes(monkeypatch, handle)
    async with aclosing(HttpxProviderHTTPTransport()) as transport:
        provider = OpenAIChatCompletionsProvider(
            base_url="https://model.test",
            transport=transport,
            retry_policy=ProviderRetryPolicy(max_attempts=2, initial_backoff_seconds=0),
        )
        assert (await collect(provider, True))[-1].type == "done"
    assert calls == [PROXY, PROXY]


async def test_partial_stream_failure_does_not_replay_on_another_route(monkeypatch):
    calls = []
    secret = "private-header-or-response-value"

    class DroppingBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            event = delta("response.output_text.delta", 0, "msg", "partial", content_index=0)
            yield f"data: {json.dumps(event)}\n\n".encode()
            raise httpx.ReadError(secret)

    def handle(proxy, request):
        calls.append(proxy)
        return httpx.Response(200, stream=DroppingBody())

    mock_routes(monkeypatch, handle)
    async with aclosing(HttpxProviderHTTPTransport()) as transport:
        adapter = OpenAIResponsesProvider(
            base_url="https://model.test",
            headers={"x-test": {"source": "literal", "value": secret}},
            transport=transport,
        )
        events = await collect(adapter, True)
    assert calls == [PROXY]
    assert [event.type for event in events] == ["text_delta", "error"]
    assert events[0].text == "partial"
    assert events[-1].error_code == "provider_read_error"
    assert secret not in repr(events)


async def test_borrowed_client_remains_usable_after_transport_close():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200))
    ) as client:
        async with aclosing(HttpxProviderHTTPTransport(client)) as transport:
            assert (
                await transport.post_json(
                    url="https://model.test", headers={}, payload={}, timeout=1
                )
                == {}
            )
        assert (await client.get("https://model.test")).is_success
