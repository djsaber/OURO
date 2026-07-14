from __future__ import annotations

import json

import pytest

from ouro.application.configuration import ConfigurationFacade
from ouro.core.errors import ConfigError


def test_configuration_overview_exposes_typed_defaults(tmp_path) -> None:
    overview = ConfigurationFacade(tmp_path).overview()

    assert overview.config_path.endswith("config.json")
    assert overview.providers.default_provider_id == "faux"
    assert overview.providers.providers[0].credential.state == "not_required"
    assert overview.prompt.configured is False
    assert overview.permissions.tools == {}
    assert overview.tool_execution.mode == "sequential"
    assert overview.queued_inputs.consumption_mode == "one_by_one"
    assert overview.run_budget.max_model_rounds is None
    assert overview.compaction.model.has_dedicated_endpoint is False


def test_provider_commands_return_safe_views_and_explicit_effect_scope(
    tmp_path,
    monkeypatch,
) -> None:
    facade = ConfigurationFacade(tmp_path)
    monkeypatch.delenv("GLM_API_KEY", raising=False)

    saved = facade.save_provider(
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
        supports_reasoning=True,
        supported_thinking_levels=("low", "high"),
        default_thinking_level="high",
        make_default=True,
    )
    assert endpoints.default_endpoint == "reasoning"
    assert endpoints.endpoints[0].supports_reasoning is True

    facade.use_provider("glm")
    monkeypatch.setenv("GLM_API_KEY", "top-secret")
    provider = facade.provider()

    assert provider.provider_id == "glm"
    assert provider.credential.reference == "env:GLM_API_KEY"
    assert provider.credential.state == "configured"
    assert "top-secret" not in provider.model_dump_json()


def test_inline_provider_credential_is_never_exposed(tmp_path) -> None:
    facade = ConfigurationFacade(tmp_path)
    facade.save_provider(
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
    facade.save_provider(
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
