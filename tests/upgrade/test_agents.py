from __future__ import annotations

import copy
import hashlib
import json

import pytest

from ouro.config.agents import AgentDefinitionsConfigManager
from ouro.core.agents import AgentDefinition
from ouro.core.errors import InvalidConfigDataError
from ouro.tui.app import OuroTuiApp
from ouro.tui.recovery import DataRecoveryApp
from ouro_upgrade.configuration import convert_agents
from ouro_upgrade.engine import upgrade
from ouro_upgrade.errors import UpgradeDataError
from ouro_upgrade.units import UpgradeUnit
from tests.upgrade.test_recovery import wait_until


@pytest.fixture
def agent_home(tmp_path):
    definition = {
        "schema_version": 1,
        "agent_id": "researcher",
        "name": "Researcher",
        "description": "Research a topic",
        "system_prompt": "Keep 中文 and user instructions intact.",
        "tool_selection": {"mode": "selected", "names": ["Read"]},
        "model": "provider/endpoint",
        "default_budget": {
            "max_model_rounds": 12,
            "max_input_tokens": None,
            "max_output_tokens": None,
            "max_elapsed_seconds": None,
        },
        "can_spawn": False,
    }
    # Persisted source content is independent of defaults in the current model.
    encoded = json.dumps(definition, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    definition["contract_hash"] = "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()
    raw = {
        "agents": {
            "revision": 7,
            "disabled_agent_ids": ["researcher"],
            "definitions": [definition],
            "runtime": {
                "max_depth": 1,
                "spawn_policy": {
                    "allowed_lifecycle_bindings": ["bound"],
                    "allow_write": False,
                    "allowed_agent_id_patterns": ["research*"],
                },
            },
        },
    }
    (tmp_path / "config").mkdir()
    (tmp_path / "config/agents.json").write_text(json.dumps(raw), encoding="utf-8")
    return tmp_path


def test_agent_upgrade_preserves_content_and_policy_without_rewriting_current_data(agent_home):
    path = agent_home / "config/agents.json"
    original = path.read_bytes()
    raw = json.loads(original)
    before = copy.deepcopy(raw)
    with pytest.raises(InvalidConfigDataError):
        AgentDefinitionsConfigManager(agent_home).snapshot()
    assert upgrade(agent_home, check=True)["status"] == "upgrade_required"
    assert path.read_bytes() == original
    assert convert_agents(raw) == convert_agents(convert_agents(raw))
    assert raw == before

    report = upgrade(agent_home)
    assert report["files"] == ["config/agents.json"]
    config = AgentDefinitionsConfigManager(agent_home).snapshot()
    assert config.revision == 7
    assert config.disabled_agent_ids == ("researcher",)
    expected = dict(raw["agents"]["definitions"][0])
    expected.pop("can_spawn")
    expected.pop("contract_hash")
    assert config.definitions[0] == AgentDefinition.model_validate(expected)
    assert config.runtime.spawn_policy.allow_write is False
    assert config.runtime.spawn_policy.allowed_agent_id_patterns == ("research*",)
    assert (agent_home / report["backup"] / "0").read_bytes() == original
    current = path.read_bytes()
    assert upgrade(agent_home)["status"] == "current"
    assert path.read_bytes() == current


@pytest.mark.parametrize("damage", ["hash", "current_hash", "depth", "lifecycle", "shape"])
def test_invalid_agents_are_isolated_and_deleted_only_when_selected(agent_home, damage):
    path = agent_home / "config/agents.json"
    raw = json.loads(path.read_bytes())
    if damage == "current_hash":
        raw = convert_agents(raw)
    if damage in ("hash", "current_hash"):
        raw["agents"]["definitions"][0]["system_prompt"] = "changed after hashing"
    elif damage == "depth":
        raw["agents"]["runtime"]["max_depth"] = 2
    elif damage == "lifecycle":
        raw["agents"]["runtime"]["spawn_policy"]["allowed_lifecycle_bindings"] = ["detached"]
    else:
        raw["agents"]["definitions"] = None
    path.write_text(json.dumps(raw), encoding="utf-8")
    original = path.read_bytes()
    unrelated = agent_home / "config/credentials.json"
    unrelated.write_bytes(b"untouched")
    with pytest.raises(UpgradeDataError):
        upgrade(agent_home)
    report = upgrade(agent_home, isolate_failures=True)
    assert report["status"] == "partial"
    assert report["failures"][0]["unit"] == {
        "kind": "configuration",
        "identity": "config/agents.json",
    }
    assert report["failures"][0]["discardable"] is True
    assert path.read_bytes() == original
    result = upgrade(
        agent_home,
        isolate_failures=True,
        discard=frozenset({UpgradeUnit("configuration", "config/agents.json")}),
    )
    assert result["status"] == "upgraded"
    assert not path.exists()
    assert unrelated.read_bytes() == b"untouched"
    assert (agent_home / result["backup"] / "0").read_bytes() == original
    assert AgentDefinitionsConfigManager(agent_home).snapshot().definitions == ()


async def test_agent_startup_routes_to_offline_recovery(agent_home):
    app = OuroTuiApp(home=agent_home)
    async with app.run_test(size=(100, 32)):
        await wait_until(lambda: app.startup_data_error is not None)
    assert isinstance(app.startup_data_error, InvalidConfigDataError)
    recovery = DataRecoveryApp(
        home=agent_home, error=str(app.startup_data_error), auto_upgrade=True
    )
    async with recovery.run_test(size=(100, 32)):
        await wait_until(lambda: recovery.return_value == "upgraded")
    assert (
        AgentDefinitionsConfigManager(agent_home).snapshot().definitions[0].agent_id == "researcher"
    )
