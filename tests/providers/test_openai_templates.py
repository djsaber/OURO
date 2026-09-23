from __future__ import annotations

import json

import httpx
import pytest

from ouro.config import ProviderConfigManager, ProviderRuntimeOptions
from ouro.config.provider_templates import get_provider_template
from ouro.core.context import ContextView
from ouro.core.messages import ModelMessage
from ouro.core.reasoning import ReasoningRequest, resolve_reasoning
from ouro.core.tools import ToolDefinition
from ouro.providers import build_runtime_adapter
from ouro.providers.http_transport import HttpxProviderHTTPTransport
from tests.providers.responses_fixtures import message, terminal


@pytest.mark.parametrize(
    "model_id",
    [
        "gpt-5.5",
        "gpt-5.6-luna",
        "gpt-5.6-terra",
        "gpt-5.6-sol",
        "gpt-6-astra",
        "gpt-6-sol",
        "gpt-6-luna",
    ],
)
async def test_openai_preset_sends_native_responses_tools_and_reasoning(
    tmp_path, monkeypatch, model_id
):
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-key")
    manager = ProviderConfigManager(tmp_path)
    provider = manager.create_from_template(get_provider_template("openai"))
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
            "ouro.providers.openai_responses.HttpxProviderHTTPTransport",
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
                    ContextView(
                        model_messages=[ModelMessage.text(role="user", text="Reply OK.")],
                        tool_definitions=[
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
    assert payload["reasoning"] == {"effort": "high"}
    assert payload["tools"][0]["name"] == "echo"
    assert payload["max_output_tokens"] == 32768
    assert payload["store"] is False
    assert "reasoning.encrypted_content" in payload["include"]
    assert (
        not {"messages", "reasoning_effort", "max_tokens", "temperature", "top_p"} & payload.keys()
    )


@pytest.mark.parametrize("template_id", ["openai", "openai-codex"])
def test_model_effort_limits_are_enforced_before_requests(template_id):
    template = get_provider_template(template_id)
    astra = template.endpoint("gpt-6-astra").reasoning.capability
    with pytest.raises(ValueError, match="always on"):
        resolve_reasoning(astra, ReasoningRequest(mode="disabled"))
    old = template.endpoint("gpt-5.5").reasoning.capability
    with pytest.raises(ValueError, match="not supported"):
        resolve_reasoning(old, ReasoningRequest(effort="max"))
    for model_id in (
        "gpt-6-astra",
        "gpt-6-sol",
        "gpt-6-luna",
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
    ):
        capability = template.endpoint(model_id).reasoning.capability
        assert resolve_reasoning(capability, ReasoningRequest(effort="max")).variant == "max"
        with pytest.raises(ValueError, match="not supported"):
            resolve_reasoning(capability, ReasoningRequest(effort="minimal"))


def test_api_none_is_not_assumed_to_be_a_subscription_capability():
    api = get_provider_template("openai").endpoint("gpt-6-sol").reasoning
    assert (
        resolve_reasoning(api.capability, ReasoningRequest(mode="disabled")).variant == "disabled"
    )
    assert api.profile.variants["disabled"] == {"reasoning": {"effort": "none"}}
    subscription = get_provider_template("openai-codex").endpoint("gpt-6-sol").reasoning
    with pytest.raises(ValueError, match="always on"):
        resolve_reasoning(subscription.capability, ReasoningRequest(mode="disabled"))
