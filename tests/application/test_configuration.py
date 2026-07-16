from __future__ import annotations

import json

import pytest

from ouro.application.configuration import ConfigurationFacade
from ouro.config import openai_effort_reasoning
from ouro.core.errors import ConfigError
from ouro.mcp import MCPServerConfigUpdate


def test_configuration_overview_exposes_typed_defaults(tmp_path) -> None:
    overview = ConfigurationFacade(tmp_path).overview()

    assert overview.config_path.endswith("config.json")
    assert overview.providers.default_provider_id is None
    assert overview.providers.providers == []
    assert overview.prompt.configured is False
    assert overview.permissions.tools == {}
    assert overview.tool_execution.mode == "sequential"
    assert overview.queued_inputs.consumption_mode == "one_by_one"
    assert overview.run_budget.max_model_rounds is None
    assert overview.compaction.model.has_dedicated_endpoint is False
    assert overview.mcp.servers == []


def test_mcp_configuration_is_exposed_only_through_redacted_application_views(
    tmp_path,
) -> None:
    facade = ConfigurationFacade(tmp_path)
    catalog = facade.save_mcp_server(
        "remote",
        MCPServerConfigUpdate(
            name="Remote",
            transport="http",
            url="https://mcp.example.test/service?token=url-secret",
            headers={"Authorization": "Bearer header-secret"},
            credential_refs={"Authorization": "inline:credential-secret"},
        ),
    )

    payload = catalog.model_dump_json()
    server = facade.mcp_server("remote")
    assert "url-secret" not in payload
    assert "header-secret" not in payload
    assert "credential-secret" not in payload
    assert server.url == "https://mcp.example.test/service"
    assert server.headers == {"Authorization": "***"}
    assert server.credential_refs == {"Authorization": "inline:***"}


def test_provider_commands_return_safe_views_and_explicit_effect_scope(
    tmp_path,
    monkeypatch,
) -> None:
    facade = ConfigurationFacade(tmp_path)
    monkeypatch.delenv("GLM_API_KEY", raising=False)

    saved = facade.add_provider(
        "glm",
        provider_type="openai-compatible",
        base_url="https://example.test/v1",
        api_key_env="GLM_API_KEY",
    )
    assert saved.change is not None
    assert saved.change.effective_from == "next_request"
    assert facade.provider("glm").credential.state == "missing"

    endpoints = facade.save_endpoint(
        "glm",
        "reasoning",
        model="model-name",
        reasoning=openai_effort_reasoning(
            supported_efforts=("low", "high"),
            default_effort="high",
        ),
        make_default=True,
    )
    assert endpoints.default_endpoint == "reasoning"
    assert endpoints.endpoints[0].reasoning.capability.default.effort == "high"

    facade.use_provider("glm")
    monkeypatch.setenv("GLM_API_KEY", "top-secret")
    provider = facade.provider()

    assert provider.provider_id == "glm"
    assert provider.credential.reference == "env:GLM_API_KEY"
    assert provider.credential.state == "configured"
    assert "top-secret" not in provider.model_dump_json()


def test_inline_provider_credential_is_never_exposed(tmp_path) -> None:
    facade = ConfigurationFacade(tmp_path)
    facade.add_provider(
        "local",
        provider_type="openai-compatible",
        base_url="https://example.test/v1",
        api_key="inline-secret",
    )

    payload = facade.provider("local").model_dump_json()

    assert "inline-secret" not in payload
    assert "inline:***" in payload


def test_configuration_section_commands_preserve_other_sections(tmp_path) -> None:
    facade = ConfigurationFacade(tmp_path)

    prompt = facade.set_system_prompt("Custom prompt")
    permissions = facade.set_tool_permission("Bash", "approval")
    execution = facade.update_tool_execution(mode="parallel", max_concurrency=3)
    queued = facade.update_queued_inputs(consumption_mode="all_at_once")
    budget = facade.update_run_budget(max_model_rounds=8, max_elapsed_seconds=60)
    compaction = facade.update_compaction_model(
        temperature=0.1,
        max_output_tokens=2048,
    )

    assert prompt.change is not None and prompt.change.effective_from == "next_runtime"
    assert permissions.tools == {"Bash": "approval"}
    assert execution.mode == "parallel"
    assert queued.consumption_mode == "all_at_once"
    assert budget.max_model_rounds == 8
    assert compaction.model.max_output_tokens == 2048

    overview = ConfigurationFacade(tmp_path).overview()
    assert overview.prompt.system_prompt == "Custom prompt"
    assert overview.permissions.tools == {"Bash": "approval"}
    assert overview.tool_execution.max_concurrency == 3
    assert overview.queued_inputs.consumption_mode == "all_at_once"
    assert overview.run_budget.max_elapsed_seconds == 60
    assert overview.compaction.model.temperature == 0.1


def test_invalid_configuration_mutation_does_not_change_persisted_state(tmp_path) -> None:
    facade = ConfigurationFacade(tmp_path)
    facade.add_provider(
        "glm",
        provider_type="openai-compatible",
        base_url="https://example.test/v1",
    )
    config_path = tmp_path / "config" / "config.json"
    before = json.loads(config_path.read_text(encoding="utf-8"))

    with pytest.raises(ConfigError):
        facade.save_endpoint(
            "glm",
            "invalid",
            model="model-name",
            context_window=1024,
            max_output_tokens=2048,
        )

    assert json.loads(config_path.read_text(encoding="utf-8")) == before


def test_provider_templates_are_typed_and_apply_provider_endpoint_atomically(
    tmp_path,
) -> None:
    facade = ConfigurationFacade(tmp_path)
    facade.set_system_prompt("Keep this section")

    templates = facade.provider_template_catalog()
    assert [template.template_id for template in templates.templates] == [
        "openai",
        "anthropic",
        "gemini",
        "deepseek",
        "qwen",
        "kimi",
        "glm",
        "openrouter",
        "mistral",
    ]
    assert facade.provider_template("anthropic").provider_type == "anthropic"
    assert facade.provider_template("anthropic").protocol == "anthropic-messages"
    assert facade.provider_template("kimi").default_endpoint == "kimi-k2.7-code"
    assert facade.provider_template("kimi").endpoint_count == 3

    configured = facade.add_provider("gemini", provider_type="gemini", make_default=True)
    provider = next(item for item in configured.providers if item.provider_id == "gemini")

    assert configured.default_provider_id == "gemini"
    assert configured.change is not None
    assert configured.change.action == "created"
    assert provider.default_endpoint == "gemini-3.5-flash"
    assert {endpoint.endpoint_id for endpoint in provider.endpoints} == {
        "gemini-3.5-flash",
        "gemini-3.1-pro-preview",
        "gemini-3.1-flash-lite",
    }
    assert facade.endpoint("gemini", "gemini-3.5-flash").model_id == "gemini-3.5-flash"
    assert provider.credential.reference == "env:GEMINI_API_KEY"
    assert ConfigurationFacade(tmp_path).system_prompt().system_prompt == "Keep this section"


def test_provider_template_reapply_preserves_other_endpoints_and_credential(tmp_path) -> None:
    facade = ConfigurationFacade(tmp_path)
    facade.add_provider(
        "primary",
        provider_type="openai",
        api_key_env="CUSTOM_OPENAI_KEY",
    )
    facade.save_endpoint("primary", "legacy", model="gpt-legacy")
    facade.update_provider("primary", enabled=False)

    facade.update_provider("primary", model="gpt-custom")
    provider = facade.provider("primary")

    assert provider.credential.reference == "env:CUSTOM_OPENAI_KEY"
    assert provider.enabled is False
    assert provider.default_endpoint == "gpt-5.6"
    assert {endpoint.endpoint_id for endpoint in provider.endpoints} == {
        "gpt-5.6",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        "legacy",
    }
    assert facade.endpoint("primary", "gpt-5.6").model_id == "gpt-custom"


def test_provider_template_default_endpoint_override_keeps_other_template_endpoints(
    tmp_path,
) -> None:
    facade = ConfigurationFacade(tmp_path)

    facade.add_provider(
        "openai",
        provider_type="openai",
        endpoint_id="primary",
        model="gpt-custom",
    )
    provider = facade.provider("openai")

    assert provider.default_endpoint == "primary"
    assert {endpoint.endpoint_id for endpoint in provider.endpoints} == {
        "primary",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
    }
    assert facade.endpoint("openai", "primary").model_id == "gpt-custom"


def test_provider_template_rejects_default_endpoint_collision_before_write(tmp_path) -> None:
    facade = ConfigurationFacade(tmp_path)

    with pytest.raises(ConfigError, match="conflicts with another template endpoint"):
        facade.add_provider(
            "openai",
            provider_type="openai",
            endpoint_id="gpt-5.6-terra",
        )

    assert not (tmp_path / "config" / "config.json").exists()


def test_integrated_provider_update_is_detected_from_persisted_type(tmp_path) -> None:
    facade = ConfigurationFacade(tmp_path)
    facade.add_provider("primary", provider_type="anthropic")
    facade.update_provider("primary", model="claude-custom")

    provider = facade.provider("primary")
    assert provider.provider_type == "anthropic"
    assert provider.protocol == "anthropic-messages"
    assert facade.endpoint("primary", "claude-sonnet-5").model_id == "claude-custom"


def test_provider_create_conflict_and_explicit_replace_are_atomic(tmp_path) -> None:
    facade = ConfigurationFacade(tmp_path)
    facade.add_provider("primary", provider_type="anthropic")
    config_path = tmp_path / "config" / "config.json"
    before = config_path.read_text(encoding="utf-8")

    with pytest.raises(ConfigError, match="provider already exists"):
        facade.add_provider("primary", provider_type="openai")

    assert config_path.read_text(encoding="utf-8") == before

    replaced = facade.replace_provider("primary", provider_type="openai")
    provider = next(item for item in replaced.providers if item.provider_id == "primary")
    assert provider.provider_type == "openai"
    assert provider.protocol == "openai-chat-completions"
    assert {endpoint.endpoint_id for endpoint in provider.endpoints} == {
        "gpt-5.6",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
    }


def test_skill_queries_and_management_use_application_dtos(tmp_path) -> None:
    skill_dir = tmp_path / "skills" / "echo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: echo\ndescription: Echo guidance\n---\n\nUse echo carefully.\n",
        encoding="utf-8",
    )
    facade = ConfigurationFacade(tmp_path)

    catalog = facade.skill_catalog()
    loaded = facade.skill("echo")
    disabled = facade.set_skill_enabled("echo", enabled=False)

    assert [skill.name for skill in catalog.skills] == ["echo"]
    assert loaded.skill.name == "echo"
    assert "Use echo carefully" in loaded.content
    assert disabled.change.section == "skills"
    assert facade.skill_policy().disabled == ["echo"]
    assert facade.skill_catalog().skills == []
    assert facade.skill_catalog(include_disabled=True).skills[0].enabled is False
