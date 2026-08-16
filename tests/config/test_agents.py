from __future__ import annotations

import json

import pytest

from ouro.config.agents import (
    MAX_USER_AGENT_DEFINITIONS,
    AgentDefinitionsConfigManager,
    AgentRuntimeConfig,
)
from ouro.core.agents import AgentDefinition, AgentSpawnPolicy, ToolSelection
from ouro.core.errors import ConfigError


def _definition(agent_id: str = "reviewer") -> AgentDefinition:
    return AgentDefinition(
        agent_id=agent_id,
        name="Reviewer",
        description="Review a bounded task.",
        system_prompt="Inspect evidence before reaching a conclusion.",
        tool_selection=ToolSelection(mode="selected", names=("Read",)),
    )


def test_agent_definition_config_crud_uses_expected_revision(tmp_path) -> None:
    manager = AgentDefinitionsConfigManager(tmp_path)
    assert manager.snapshot().revision == 0

    created = manager.create(_definition(), expected_revision=0)
    assert created.revision == 1
    assert [item.agent_id for item in created.definitions] == ["reviewer"]

    renamed = AgentDefinition(
        **{
            **_definition().model_dump(exclude={"contract_hash"}),
            "name": "Code reviewer",
        }
    )
    updated = manager.update(renamed, expected_revision=1)
    assert updated.revision == 2
    assert updated.definitions[0].name == "Code reviewer"

    with pytest.raises(ConfigError, match="revision conflict"):
        manager.delete("reviewer", expected_revision=1)
    deleted = manager.delete("reviewer", expected_revision=2)
    assert deleted.revision == 3
    assert deleted.definitions == ()


def test_agent_definition_config_rejects_builtin_override_and_ignores_unknown_fields(
    tmp_path,
) -> None:
    manager = AgentDefinitionsConfigManager(tmp_path)

    with pytest.raises(ConfigError, match="reserved"):
        manager.create(_definition("general"), expected_revision=0)

    path = tmp_path / "config" / "config.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "agents": {
                    "revision": 0,
                    "definitions": [{**_definition().model_dump(mode="json"), "extra": True}],
                }
            }
        ),
        encoding="utf-8",
    )
    snapshot = manager.snapshot()

    assert [item.agent_id for item in snapshot.definitions] == ["reviewer"]
    assert "extra" not in snapshot.definitions[0].model_dump()


def test_agent_definition_mutation_preserves_other_config_sections(tmp_path) -> None:
    path = tmp_path / "config" / "config.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"prompts":{"system":"keep"}}', encoding="utf-8")

    AgentDefinitionsConfigManager(tmp_path).create(_definition(), expected_revision=0)

    assert json.loads(path.read_text(encoding="utf-8"))["prompts"] == {"system": "keep"}


def test_agent_definition_limit_is_rejected_before_persisting_invalid_config(tmp_path) -> None:
    manager = AgentDefinitionsConfigManager(tmp_path)
    for revision in range(MAX_USER_AGENT_DEFINITIONS):
        manager.create(_definition(f"reviewer-{revision}"), expected_revision=revision)

    with pytest.raises(ConfigError, match="invalid agents config"):
        manager.create(
            _definition("one-too-many"),
            expected_revision=MAX_USER_AGENT_DEFINITIONS,
        )

    snapshot = manager.snapshot()
    assert snapshot.revision == MAX_USER_AGENT_DEFINITIONS
    assert len(snapshot.definitions) == MAX_USER_AGENT_DEFINITIONS


def test_agent_runtime_config_defaults_and_update_preserve_definitions(tmp_path) -> None:
    manager = AgentDefinitionsConfigManager(tmp_path)
    initial = manager.snapshot()
    assert initial.runtime.max_depth == 1
    assert initial.runtime.spawn_policy.allowed_agent_id_patterns == ("*",)

    created = manager.create(_definition(), expected_revision=0)
    updated = manager.update_runtime(
        AgentRuntimeConfig(
            spawn_policy=AgentSpawnPolicy(
                allowed_agent_id_patterns=("review*",),
            ),
        ),
        expected_revision=created.revision,
    )

    assert updated.revision == 2
    assert updated.definitions == created.definitions
    assert updated.runtime.spawn_policy.allowed_agent_id_patterns == ("review*",)
    with pytest.raises(ConfigError, match="revision conflict"):
        manager.update_runtime(AgentRuntimeConfig(), expected_revision=1)


def test_agent_runtime_config_rejects_unsupported_first_release_modes(tmp_path) -> None:
    manager = AgentDefinitionsConfigManager(tmp_path)
    path = tmp_path / "config" / "config.json"
    path.parent.mkdir(parents=True)

    for runtime in (
        {"max_depth": 2},
        {"spawn_policy": {"allowed_lifecycle_bindings": ["bound", "detached"]}},
    ):
        path.write_text(
            json.dumps({"agents": {"revision": 0, "runtime": runtime}}),
            encoding="utf-8",
        )
        with pytest.raises(ConfigError, match="invalid agents config"):
            manager.snapshot()
