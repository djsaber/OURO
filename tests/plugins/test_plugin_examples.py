from __future__ import annotations

from pathlib import Path

import pytest

from ouro.local_application import open_local_plugins

EXAMPLES = Path(__file__).parents[2] / "examples" / "plugins"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("directory", "plugin_id"),
    [
        ("declarative-skill", "dev.ouro.examples.declarative_skill"),
        ("inprocess-command", "dev.ouro.examples.inprocess_command"),
        ("inprocess-observer", "dev.ouro.examples.observer"),
        ("inprocess-prompt-policy", "dev.ouro.examples.prompt_policy"),
        ("worker-tool", "dev.ouro.examples.worker_tool"),
    ],
)
async def test_documented_plugin_examples_activate(
    tmp_path,
    directory: str,
    plugin_id: str,
) -> None:
    async with open_local_plugins(tmp_path / "home") as service:
        validation = service.validate(EXAMPLES / directory)
        assert validation.valid is True
        service.install(EXAMPLES / directory)

        result = await service.enable(plugin_id)

        assert result.status == "active"
        assert service.show(plugin_id).active_generations[0]["execution_contract_hash"]
