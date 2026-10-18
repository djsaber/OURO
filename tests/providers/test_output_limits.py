from __future__ import annotations

import json

import httpx
import pytest

from ouro.core.provider import ModelMetadata
from ouro.providers.http_transport import HttpxProviderHTTPTransport
from ouro.providers.openai_chat_completions import OpenAIChatCompletionsProvider
from ouro.providers.openai_responses import OpenAIResponsesProvider
from tests.providers.test_request_authentication import response


@pytest.mark.parametrize("provider", [OpenAIChatCompletionsProvider, OpenAIResponsesProvider])
@pytest.mark.parametrize("limit", [None, 256])
@pytest.mark.parametrize("streaming", [False, True])
async def test_output_limit_is_encoded_only_when_configured(provider, limit, streaming):
    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        return response(provider, streaming)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        adapter = provider(
            base_url="https://example.test",
            transport=HttpxProviderHTTPTransport(client),
        )
        events = [
            event
            async for event in adapter.stream(
                model=ModelMetadata(
                    provider_id="test",
                    model_id="test",
                    supports_streaming=streaming,
                    max_output_tokens=limit,
                ),
                messages=[],
            )
        ]
    assert events[-1].type == "done"
    assert len(requests) == 1
    caps = {k: v for k, v in requests[0].items() if k in {"max_tokens", "max_output_tokens"}}
    key = "max_output_tokens" if provider is OpenAIResponsesProvider else "max_tokens"
    assert caps == ({} if limit is None else {key: limit})
