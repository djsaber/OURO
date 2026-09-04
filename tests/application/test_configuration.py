from __future__ import annotations

import json

import pytest

from ouro.application.configuration import ConfigurationFacade
from ouro.application.configuration_models import CompactionConfigInput
from ouro.config import openai_effort_reasoning
from ouro.core.errors import ConfigError
from ouro.core.reasoning import ReasoningRequest
from ouro.local_application import LocalApplication
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
    assert overview.agents.revision == 0
    assert overview.agents.max_depth == 1
    assert overview.agents.spawn_policy.allowed_agent_id_patterns == ("*",)
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
    plugin_permissions = facade.set_plugin_permission("dev.ouro.deploy", "deny")
    execution = facade.update_tool_execution(mode="parallel", max_concurrency=3)
    queued = facade.update_queued_inputs(consumption_mode="all_at_once")
    budget = facade.update_run_budget(max_model_rounds=8, max_elapsed_seconds=60)
    agents = facade.update_agent_runtime(
        expected_revision=0,
        allowed_agent_id_patterns=("general", "review-*"),
    )
    compaction = facade.update_compaction_model(
        provider_id="faux",
        endpoint_id="summary",
        temperature=0.1,
        max_output_tokens=2048,
    )

    assert prompt.change is not None and prompt.change.effective_from == "next_runtime"
    assert permissions.tools == {"Bash": "approval"}
    assert plugin_permissions.plugins == {"dev.ouro.deploy": "deny"}
    assert execution.mode == "parallel"
    assert queued.consumption_mode == "all_at_once"
    assert budget.max_model_rounds == 8
    assert agents.revision == 1
    assert agents.spawn_policy.allowed_agent_id_patterns == ("general", "review-*")
    assert agents.change is not None
    assert agents.change.effective_from == "next_runtime"
    assert compaction.model.max_output_tokens == 2048

    overview = ConfigurationFacade(tmp_path).overview()
    assert overview.prompt.system_prompt == "Custom prompt"
    assert overview.permissions.tools == {"Bash": "approval"}
    assert overview.permissions.plugins == {"dev.ouro.deploy": "deny"}
    assert overview.tool_execution.max_concurrency == 3
    assert overview.queued_inputs.consumption_mode == "all_at_once"
    assert overview.run_budget.max_elapsed_seconds == 60
    assert overview.compaction.model.temperature == 0.1


def test_complete_compaction_configuration_is_saved_atomically(tmp_path) -> None:
    facade = ConfigurationFacade(tmp_path)

    saved = facade.save_compaction(
        CompactionConfigInput(
            strategy_id="builtin.llm_summary",
            auto_enabled=False,
            trigger_ratio=0.75,
            target_ratio=0.25,
            provider_id="faux",
            endpoint_id="summary",
            temperature=0.2,
            max_output_tokens=2048,
        )
    )

    assert saved.policy == {
        "auto_enabled": False,
        "trigger_ratio": 0.75,
        "target_ratio": 0.25,
    }
    assert saved.model.provider_id == "faux"
    assert saved.model.max_output_tokens == 2048
    assert saved.change is not None and saved.change.effective_from == "next_runtime"

    with pytest.raises(ConfigError, match="target_ratio"):
        facade.save_compaction(
            CompactionConfigInput(
                strategy_id="builtin.llm_summary",
                trigger_ratio=0.4,
                target_ratio=0.5,
            )
        )
    assert facade.compaction_config() == saved.model_copy(update={"change": None})


def test_compaction_overrides_are_validated_against_fixed_endpoint_capabilities(tmp_path) -> None:
    facade = ConfigurationFacade(tmp_path)
    facade.add_provider(
        "glm",
        provider_type="openai-compatible",
        base_url="https://example.test/v1",
    )
    facade.save_endpoint(
        "glm",
        "summary",
        model="summary-model",
        context_window=32_000,
        max_output_tokens=2048,
        reasoning=openai_effort_reasoning(
            supported_efforts=("low", "high"),
            default_effort="low",
        ),
    )

    saved = facade.update_compaction_model(
        provider_id="glm",
        endpoint_id="summary",
        max_output_tokens=1024,
        reasoning=ReasoningRequest(mode="enabled", effort="high"),
    )

    assert saved.model.max_output_tokens == 1024
    with pytest.raises(ConfigError, match="output limit"):
        facade.update_compaction_model(max_output_tokens=4096)
    with pytest.raises(ConfigError, match="is not supported"):
        facade.update_compaction_model(reasoning=ReasoningRequest(mode="enabled", effort="medium"))
    assert facade.compaction_config() == saved.model_copy(update={"change": None})


def test_invalid_agent_runtime_update_does_not_change_persisted_state(tmp_path) -> None:
    facade = ConfigurationFacade(tmp_path)
    before = facade.agent_runtime_config()

    with pytest.raises(ConfigError, match="invalid Agent runtime config"):
        facade.update_agent_runtime(expected_revision=0, max_depth=2)

    after = facade.agent_runtime_config()
    assert after == before


def test_agent_runtime_update_exposes_the_complete_spawn_policy(tmp_path) -> None:
    facade = ConfigurationFacade(tmp_path)

    updated = facade.update_agent_runtime(
        expected_revision=0,
        allowed_agent_id_patterns=("review-*",),
        allowed_model_patterns=("trusted/*",),
        allowed_execution_modes=("async",),
        allowed_lifecycle_bindings=("bound",),
        allow_write=False,
        allow_network=False,
        allow_mcp=False,
        allow_external_paths=False,
    )

    assert updated.revision == 1
    assert updated.spawn_policy.allowed_agent_id_patterns == ("review-*",)
    assert updated.spawn_policy.allowed_model_patterns == ("trusted/*",)
    assert updated.spawn_policy.allowed_execution_modes == ("async",)
    assert updated.spawn_policy.allowed_lifecycle_bindings == ("bound",)
    assert updated.spawn_policy.allow_write is False
    assert updated.spawn_policy.allow_network is False
    assert updated.spawn_policy.allow_mcp is False
    assert updated.spawn_policy.allow_external_paths is False


@pytest.mark.asyncio
async def test_agent_runtime_config_is_applied_by_local_composition_root(tmp_path) -> None:
    ConfigurationFacade(tmp_path).update_agent_runtime(
        expected_revision=0,
        allowed_agent_id_patterns=("review-*",),
    )

    async with LocalApplication.build(tmp_path) as application:
        assert application.agents is not None
        assert application.agents.max_depth == 1
        assert application.agents.spawn_policy.allowed_agent_id_patterns == ("review-*",)

        handle = await application.supervisor.submit("Freeze the configured Agent catalog.")
        terminal = await application.supervisor.wait(handle)
        assert terminal.status == "completed"
        assert terminal.run_id is not None
        run = application.runtime.store.load_run(handle.session_id, terminal.run_id)
        assert run.agent_catalog is not None
        assert run.agent_catalog.entries == ()


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


def test_provider_template_catalog_exposes_deprecated_and_filters_disabled(
    tmp_path,
    monkeypatch,
) -> None:
    import ouro.config.provider_template_store as store_module
    from ouro.config.provider_templates import provider_templates

    facade = ConfigurationFacade(tmp_path)
    facade.provider_template_catalog()
    template_path = tmp_path / "config" / "provider-templates.json"
    without_mistral = tuple(
        template for template in provider_templates() if template.template_id != "mistral"
    )
    monkeypatch.setattr(store_module, "inline_provider_templates", lambda: without_mistral)

    deprecated = facade.provider_template_catalog()
    mistral = next(
        template for template in deprecated.templates if template.template_id == "mistral"
    )
    assert mistral.deprecated is True
    assert any("no longer in the inline" in notice for notice in deprecated.notices)

    monkeypatch.undo()
    root = json.loads(template_path.read_text(encoding="utf-8"))
    root["templates"]["openai"]["meta"]["enabled"] = False
    template_path.write_text(json.dumps(root), encoding="utf-8")

    catalog = facade.provider_template_catalog()

    listed_ids = [template.template_id for template in catalog.templates]
    assert "openai" not in listed_ids
    mistral = next(template for template in catalog.templates if template.template_id == "mistral")
    assert mistral.deprecated is False


def test_restore_provider_template_returns_inline_view(tmp_path) -> None:
    facade = ConfigurationFacade(tmp_path)
    template_path = tmp_path / "config" / "provider-templates.json"
    facade.provider_template_catalog()
    root = json.loads(template_path.read_text(encoding="utf-8"))
    root["templates"]["glm"]["display_name"] = "My GLM"
    template_path.write_text(json.dumps(root), encoding="utf-8")

    view = facade.restore_provider_template("glm")

    assert view.display_name == "Zhipu GLM"
    restored = json.loads(template_path.read_text(encoding="utf-8"))
    assert restored["templates"]["glm"]["display_name"] == "Zhipu GLM"


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
        "grok",
        "openrouter",
        "mistral",
    ]
    assert facade.provider_template("anthropic").provider_type == "anthropic"
    assert facade.provider_template("anthropic").protocol == "anthropic-messages"
    assert facade.provider_template("kimi").default_endpoint == "kimi-k3"
    assert facade.provider_template("kimi").endpoint_count == 4

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


def test_provider_update_returns_template_merge_report_and_tracking_state(tmp_path) -> None:
    facade = ConfigurationFacade(tmp_path)
    facade.add_provider("primary", provider_type="openai")
    template_path = tmp_path / "config" / "provider-templates.json"
    root = json.loads(template_path.read_text(encoding="utf-8"))
    template = root["templates"]["openai"]
    from_revision = template["revision"]
    template["revision"] = from_revision + 1
    endpoint = next(item for item in template["endpoints"] if item["endpoint_id"] == "gpt-5.6")
    endpoint["context_window"] = 2_000_000
    template_path.write_text(json.dumps(root), encoding="utf-8")

    updated = facade.update_provider("primary")
    provider = facade.provider("primary")

    assert updated.template_update is not None
    assert updated.template_update.template_id == "openai"
    assert updated.template_update.from_revision == from_revision
    assert updated.template_update.to_revision == from_revision + 1
    assert updated.template_update.updated_endpoints == ["gpt-5.6"]
    assert provider.template_id == "openai"
    assert provider.template_origin == "inline"
    assert provider.template_revision == from_revision + 1
    assert facade.endpoint("primary", "gpt-5.6").context_window == 2_000_000


def test_removed_provider_template_rejects_update_without_changing_provider(
    tmp_path,
    monkeypatch,
) -> None:
    import ouro.config.provider_template_store as store_module
    from ouro.config.provider_templates import provider_templates

    facade = ConfigurationFacade(tmp_path)
    facade.add_provider("primary", provider_type="openai")
    config_path = tmp_path / "config" / "config.json"
    before = json.loads(config_path.read_text(encoding="utf-8"))
    without_openai = tuple(
        template for template in provider_templates() if template.template_id != "openai"
    )
    monkeypatch.setattr(store_module, "inline_provider_templates", lambda: without_openai)

    with pytest.raises(ConfigError, match="template was removed"):
        facade.update_provider("primary")

    assert json.loads(config_path.read_text(encoding="utf-8")) == before


def test_provider_settings_update_preserves_integrated_endpoint_registry(tmp_path) -> None:
    facade = ConfigurationFacade(tmp_path)
    facade.add_provider("primary", provider_type="openai")
    facade.save_endpoint("primary", "private", model="private-model")
    before = facade.provider("primary").endpoints

    updated = facade.update_provider_settings(
        "primary",
        base_url="https://gateway.example.invalid/v1",
        api_key_env="PRIVATE_OPENAI_KEY",
        timeout=45.0,
        enabled=False,
    )
    provider = next(item for item in updated.providers if item.provider_id == "primary")

    assert provider.base_url == "https://gateway.example.invalid/v1"
    assert provider.credential.reference == "env:PRIVATE_OPENAI_KEY"
    assert provider.timeout == 45.0
    assert provider.enabled is False
    assert provider.endpoints == before
    assert updated.change is not None
    assert updated.change.action == "settings_updated"


def test_provider_settings_reject_disabling_the_default_provider(tmp_path) -> None:
    facade = ConfigurationFacade(tmp_path)
    facade.add_provider("primary", provider_type="openai", make_default=True)

    with pytest.raises(ConfigError, match="select another default provider first"):
        facade.update_provider_settings("primary", enabled=False)

    assert facade.provider("primary").enabled is True


def test_using_endpoint_atomically_updates_the_default_provider_route(tmp_path) -> None:
    facade = ConfigurationFacade(tmp_path)
    for provider_id in ("primary", "secondary"):
        facade.add_provider(
            provider_id,
            provider_type="openai-compatible",
            base_url=f"https://{provider_id}.example.invalid/v1",
        )
        facade.save_endpoint(
            provider_id,
            "chat",
            model=f"{provider_id}-model",
        )
    facade.use_provider("primary")

    endpoints = facade.use_endpoint("secondary", "chat")
    providers = facade.provider_catalog()

    assert endpoints.default_endpoint == "chat"
    assert endpoints.change is not None
    assert endpoints.change.action == "default_route_changed"
    assert providers.default_provider_id == "secondary"
    assert facade.provider("secondary").default is True


def test_using_endpoint_rejects_disabled_provider_without_partial_update(tmp_path) -> None:
    facade = ConfigurationFacade(tmp_path)
    facade.add_provider("primary", provider_type="openai", make_default=True)
    facade.add_provider(
        "disabled",
        provider_type="openai-compatible",
        base_url="https://disabled.example.invalid/v1",
        enabled=False,
    )
    facade.save_endpoint("disabled", "base", model="disabled-base")
    facade.save_endpoint("disabled", "chat", model="disabled-model")
    before = facade.provider_catalog()

    with pytest.raises(ConfigError, match="provider is disabled"):
        facade.use_endpoint("disabled", "chat")

    after = facade.provider_catalog()
    assert after.default_provider_id == before.default_provider_id == "primary"
    assert facade.provider("disabled").default_endpoint == "base"


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
    assert catalog.skills[0].origin == "ouro"
    assert next(root for root in catalog.roots if root.source_id == "ouro.user").enabled is True
    assert loaded.skill.name == "echo"
    assert "Use echo carefully" in loaded.content
    assert disabled.change.section == "skills"
    assert facade.skill_policy().disabled == ["echo"]
    assert facade.skill_catalog().skills == []
    assert facade.skill_catalog(include_disabled=True).skills[0].enabled is False


def test_skill_discovery_policy_updates_application_view(tmp_path) -> None:
    facade = ConfigurationFacade(tmp_path)

    policy = facade.update_skill_discovery(
        source_updates={"ouro.user": False, "claude.workspace": False}
    )

    assert policy.change is not None
    assert policy.change.effective_from == "next_runtime"
    assert policy.disabled_sources == ["claude.workspace", "ouro.user"]
    states = {source.source_id: source.enabled for source in policy.sources}
    assert states["ouro.user"] is False
    assert states["ouro.workspace"] is True
    assert states["claude.user"] is True
    assert states["claude.workspace"] is False
