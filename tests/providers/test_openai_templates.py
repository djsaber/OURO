from __future__ import annotations

import json

import httpx
import pytest

from ouro.config import ProviderConfigManager, ProviderRuntimeOptions
from ouro.config.provider_templates import get_provider_template
from ouro.core.context import ContextBuilder
from ouro.core.messages import ModelMessage
from ouro.core.reasoning import ReasoningRequest, resolve_reasoning
from ouro.core.tools import ToolDefinition
from ouro.providers import build_runtime_adapter
from ouro.providers.http_transport import HttpxProviderHTTPTransport
from tests.providers.responses_fixtures import message, terminal


@pytest.mark.parametrize(
    "model_id",
    [endpoint.model_id for endpoint in get_provider_template("openai").endpoints],
)
async def test_openai_preset_sends_native_responses_tools_and_reasoning(
    tmp_path, monkeypatch, model_id
):
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-key")
    manager = ProviderConfigManager(tmp_path)
    provider = manager.create_from_template(
        get_provider_template("openai"), api_key_env="OPENAI_API_KEY"
    )
    endpoint = next(item for item in provider.endpoints if item.model_id == model_id)
    requests = []

    def respond(request):
        assert str(request.url) == "https://api.openai.com/v1/responses"
        assert request.headers["authorization"] == "Bearer fixture-key"
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=f"data: {json.dumps(terminal([message()]))}\n\n",
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(
            "ouro.providers.openai_responses.adapter.HttpxProviderHTTPTransport",
            lambda: HttpxProviderHTTPTransport(client),
        )
        adapter = build_runtime_adapter(
            manager.resolve_runtime_options(
                ProviderRuntimeOptions(
                    provider_id=provider.provider_id,
                    endpoint_id=endpoint.endpoint_id,
                    reasoning_request=ReasoningRequest(mode="enabled", effort="high"),
                )
            )
        )
        try:
            events = [
                event
                async for event in adapter.stream(
                    ContextBuilder().build(
                        messages=[ModelMessage.text(role="user", text="Reply OK.")],
                        tools=[
                            ToolDefinition(
                                name="echo",
                                description="Return the given value",
                                parameters_schema={"type": "object", "properties": {}},
                            )
                        ],
                    )
                )
            ]
        finally:
            await adapter.provider.close()
    assert events[-1].type == "done"
    assert not any(event.type == "error" for event in events)
    assert len(requests) == 1
    payload = requests[0]
    assert payload["model"] == model_id
    assert payload["reasoning"] == {"effort": "high", "summary": "auto"}
    assert payload["tools"][0]["name"] == "echo"
    assert "max_output_tokens" not in payload
    assert payload["store"] is False
    assert "reasoning.encrypted_content" in payload["include"]
    assert (
        not {"messages", "reasoning_effort", "max_tokens", "temperature", "top_p"} & payload.keys()
    )


@pytest.mark.parametrize("template_id", ["openai", "openai-codex"])
def test_model_effort_limits_are_enforced_before_requests(template_id):
    template = get_provider_template(template_id)
    for model_id in ("gpt-6-astra", "gpt-6.2-astra", "gpt-6.2-sol"):
        capability = template.endpoint(model_id).reasoning.capability
        with pytest.raises(ValueError, match="always on"):
            resolve_reasoning(capability, ReasoningRequest(mode="disabled"))
    max_effort = ReasoningRequest(effort="max")
    for endpoint in template.endpoints:
        capability = endpoint.reasoning.capability
        if endpoint.model_id == "gpt-5.5":
            with pytest.raises(ValueError, match="not supported"):
                resolve_reasoning(capability, max_effort)
        else:
            assert resolve_reasoning(capability, max_effort).effort_variant == "max"
        with pytest.raises(ValueError, match="not supported"):
            resolve_reasoning(capability, ReasoningRequest(effort="minimal"))


@pytest.mark.parametrize("model_id", ["gpt-6.2-astra", "gpt-6.2-sol"])
@pytest.mark.parametrize(
    ("template_id", "structured_output"), [("openai", True), ("openai-codex", False)]
)
def test_gpt_62_templates_use_native_context_and_provider_capabilities(
    template_id, model_id, structured_output
):
    endpoint = get_provider_template(template_id).endpoint(model_id)
    assert endpoint.context_window == 1_050_000
    assert endpoint.max_output_tokens is None
    assert endpoint.supports_tools
    assert endpoint.supports_image_input
    assert endpoint.supports_streaming
    assert endpoint.supports_structured_output is structured_output


def test_api_none_is_not_assumed_to_be_a_subscription_capability():
    api = get_provider_template("openai").endpoint("gpt-6-sol").reasoning
    assert (
        resolve_reasoning(api.capability, ReasoningRequest(mode="disabled")).effort_variant
        == "disabled"
    )
    assert api.profile.effort["disabled"] == {"reasoning": {"effort": "none"}}
    subscription = get_provider_template("openai-codex").endpoint("gpt-6-sol").reasoning
    with pytest.raises(ValueError, match="always on"):
        resolve_reasoning(subscription.capability, ReasoningRequest(mode="disabled"))
