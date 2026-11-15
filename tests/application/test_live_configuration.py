from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from ouro.application.configuration.live import LiveConfigurationService
from ouro.application.contracts.configuration import MCPServerConfigInput
from ouro.local_configuration import build_local_configuration


@pytest.mark.parametrize("existing", [False, True])
async def test_configuration_compensates_failed_activation_without_a_host(
    tmp_path: Path,
    existing: bool,
) -> None:
    configuration = build_local_configuration(tmp_path)
    original = (
        configuration.save_mcp_server(
            None,
            MCPServerConfigInput(name="Original", command="python", env={"TOKEN": "secret"}),
        )
        if existing
        else None
    )
    before = configuration.mcp.snapshot().servers
    reconcile = AsyncMock(side_effect=[RuntimeError("activation failed"), None])
    service = LiveConfigurationService(
        configuration,
        reconcile_mcp=reconcile,
        refresh_skills=AsyncMock(),
        available_skills=AsyncMock(return_value=()),
    )

    with pytest.raises(RuntimeError, match="activation failed"):
        await service.save_mcp_configuration(
            original.server_id if original else None,
            MCPServerConfigInput(name="Changed", command="python"),
        )

    assert configuration.mcp.snapshot().servers == before
    if original:
        restored = reconcile.call_args.args[0]
        assert restored.name == "Original"
        assert restored.env == {"TOKEN": "secret"}


async def test_configuration_compensates_failed_skill_refresh_without_a_host(
    tmp_path: Path,
) -> None:
    configuration = build_local_configuration(tmp_path)
    before = configuration.skill_policy()
    service = LiveConfigurationService(
        configuration,
        reconcile_mcp=AsyncMock(),
        refresh_skills=AsyncMock(side_effect=RuntimeError("refresh failed")),
        available_skills=AsyncMock(return_value=()),
    )

    with pytest.raises(RuntimeError, match="refresh failed"):
        await service.update_skill_discovery(source_updates={"ouro.user": False})

    assert configuration.skill_policy() == before
