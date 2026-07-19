from __future__ import annotations

from pathlib import Path

import pytest

from ouro.local_application import open_local_plugins

EXAMPLES = Path(__file__).parents[2] / "examples" / "plugins"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("directory", "plugin_id", "executable"),
    [
        ("declarative-skill", "dev.ouro.examples.declarative_skill", False),
        ("inprocess-command", "dev.ouro.examples.inprocess_command", True),
        ("worker-tool", "dev.ouro.examples.worker_tool", True),
    ],
)
async def test_documented_plugin_examples_activate(
    tmp_path,
    directory: str,
    plugin_id: str,
    executable: bool,
) -> None:
    async with open_local_plugins(tmp_path / "home") as service:
        validation = service.validate(EXAMPLES / directory)
        assert validation.valid is True
        service.install(EXAMPLES / directory)
        service.authorize(plugin_id)
        if executable:
            service.trust_code(plugin_id)

        result = await service.enable(plugin_id)

        assert result.status == "active"
        assert service.show(plugin_id).active_generations[0]["execution_contract_hash"]
