from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest
from pydantic import ValidationError

from ouro.application.configuration_views import provider_template_view
from ouro.config.provider_headers import ContextHeaderValue, LiteralHeaderValue
from ouro.config.provider_runtime import runtime_options_from_config
from ouro.config.provider_template_store import ProviderTemplateStore
from ouro.config.provider_templates import ProviderTemplate, get_provider_template
from ouro.config.providers import ProviderConfigManager
from ouro.core.messages import ModelMessage
from ouro.core.request_context import ProviderRequestContext
from ouro.providers.factory import build_runtime_adapter
from ouro.providers.http_transport import HttpxProviderHTTPTransport
from tests.providers.test_request_authentication import response

GO_ROUTES = (
    ("opencode-go", "openai-chat-completions", "/chat/completions"),
    ("opencode-go-responses", "openai-responses", "/responses"),
    ("opencode-go-messages", "anthropic-messages", "/messages"),
)


@pytest.mark.parametrize("template_id,protocol,path", GO_ROUTES)
@pytest.mark.parametrize("streaming", [False, True])
async def test_go_template_reaches_its_protocol_with_session_headers(
    tmp_path, template_id, protocol, path, streaming
):
    template = ProviderTemplateStore(tmp_path).get(template_id)
    manager = ProviderConfigManager(tmp_path)
    created = manager.create_from_template(template, api_key="test-key")
    saved = ProviderConfigManager(tmp_path).get_provider(created.provider_id)
    assert saved.protocol == protocol
    options = runtime_options_from_config(saved, endpoint_id=None)
    adapter = build_runtime_adapter(replace(options, supports_streaming=streaming))
    received = []

    def respond(request):
        received.append(request)
        return response(type(adapter.provider), streaming)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter.provider.transport = HttpxProviderHTTPTransport(client)
        for session in ("session-one", "session-two", "session-one"):
            events = [
                event
                async for event in adapter.provider.stream(
                    model=adapter.model,
                    messages=[ModelMessage.text(role="user", text="Review this code.")],
                    request_context=ProviderRequestContext({"session.id": session}),
                )
            ]
            assert events[-1].type == "done"
            assert not any(event.type == "error" for event in events)

    assert [r.headers["x-opencode-session"] for r in received] == [
        "session-one",
        "session-two",
        "session-one",
    ]
    for request in received:
        assert str(request.url) == f"https://opencode.ai/inference/go/v1{path}"
        assert request.headers["user-agent"].startswith("ouro/")
        credential_header = "x-api-key" if protocol == "anthropic-messages" else "authorization"
        assert request.headers[credential_header].endswith("test-key")
        body = json.loads(request.content)
        assert body["model"] == template.endpoint().model_id
        assert "reasoning_effort" not in body
        assert "enable_thinking" not in body
    assert saved.headers == {"x-opencode-session": ContextHeaderValue(key="session.id")}


def test_template_headers_are_creation_defaults_and_updates_preserve_instance_choices(tmp_path):
    template = get_provider_template("opencode-go")
    manager = ProviderConfigManager(tmp_path)
    created = manager.create_from_template(template)
    assert created.headers == template.headers
    assert created.headers is not template.headers

    custom = {"X-Tenant": {"source": "literal", "value": "private-value"}}
    changed = manager.update_provider(created.provider_id, headers=custom)
    updated = manager.update_from_template(template, provider_id=created.provider_id).provider
    assert updated.headers == changed.headers

    replaced = manager.replace_from_template(
        template, provider_id=created.provider_id, display_name="Go"
    )
    assert replaced.headers == template.headers
    assert manager.create_from_template(template, headers={}).headers == {}
    overridden = manager.create_from_template(template, headers=custom)
    assert overridden.headers == {"x-tenant": LiteralHeaderValue(value="private-value")}


def test_template_headers_validate_and_public_views_redact_literal_values():
    raw = get_provider_template("opencode-go").model_dump()
    raw["headers"] = {"Host": {"source": "literal", "value": "bad.test"}}
    with pytest.raises(ValidationError, match="owned by the transport"):
        ProviderTemplate.model_validate(raw)
    raw["headers"] = {"X-Session": {"source": "context", "key": "unknown"}}
    with pytest.raises(ValidationError, match="registered request context"):
        ProviderTemplate.model_validate(raw)
    raw["headers"] = {"X-Secret": {"source": "literal", "value": "private-value"}}
    view = provider_template_view(ProviderTemplate.model_validate(raw))
    assert view.headers == {"x-secret": {"source": "literal", "value": "***"}}
    assert "private-value" not in view.model_dump_json()


def test_go_limits_and_capabilities_follow_the_gateway_and_local_codecs():
    chat = get_provider_template("opencode-go")
    assert chat.endpoint("kimi-k2.6").context_window == 262_144
    assert chat.endpoint("hy4-preview").context_window == 1_024_000
    assert chat.endpoint("deepseek-v4.1-flash").metadata["provider_output_limit"] == 384_000
    assert chat.endpoint("deepseek-v4.1-flash").supports_image_input
    assert not chat.endpoint("deepseek-v4-pro").supports_image_input
    assert not chat.endpoint("mimo-v2.5-pro").supports_image_input
    assert chat.endpoint().reasoning.capability.continuation == "text"
    messages = get_provider_template("opencode-go-messages")
    assert messages.endpoint("minimax-m2.7").context_window == 204_800
    assert not messages.endpoint("minimax-m2.7").supports_image_input
    assert messages.endpoint().reasoning.capability.continuation == "opaque"
    for template_id, _, _ in GO_ROUTES:
        template = get_provider_template(template_id)
        for endpoint in template.endpoints:
            assert endpoint.supports_tools and endpoint.supports_streaming
            assert endpoint.max_output_tokens == 32_768
            assert endpoint.max_output_tokens <= endpoint.metadata["provider_output_limit"]
            assert not endpoint.supports_audio_input and not endpoint.supports_video_input
            if template.protocol == "anthropic-messages":
                assert not endpoint.supports_structured_output
