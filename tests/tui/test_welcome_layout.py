from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import Static

from ouro.tui.app import OuroTuiApp
from ouro.tui.controller import TuiSystemStatus
from ouro.tui.widgets import WelcomePanel
from tests.tui.helpers import run_ready_app, two_route_application


@pytest.mark.parametrize("populated", [False, True])
async def test_welcome_sections_stay_within_available_width_when_resized(
    tmp_path: Path, populated: bool
) -> None:
    app = OuroTuiApp(application=two_route_application(tmp_path))
    async with run_ready_app(app, size=(160, 40)) as pilot:
        welcome = app.query_one(WelcomePanel)
        if populated:
            welcome.set_system_status(
                TuiSystemStatus(
                    workspace="C:/工作区/" + "long-directory/" * 12,
                    provider_ids=("provider-with-a-long-name",) * 3,
                    usage_request_count=123456,
                    usage_priced_request_count=123450,
                    usage_unpriced_request_count=6,
                    usage_estimated_cost=1234.56,
                    usage_billing_modes=("usage_based", "subscription", "unknown"),
                )
            )
        for width in (160, 80, 64, 48, 40, 64, 160):
            await pilot.resize_terminal(width, 40)
            await pilot.pause()

            bounds = welcome.content_region
            for section in (welcome._system, welcome._lower, welcome._usage, welcome._tips):
                assert section.region.width > 0
                assert bounds.x <= section.region.x
                assert section.region.right <= bounds.right, (width, section, section.region)
            assert welcome._usage.region.right <= welcome._tips.region.x
            assert welcome._lower.region.bottom <= welcome._system.region.y
            for text in welcome._usage.query(Static):
                if text.display and text.render().cell_length > text.content_size.width:
                    assert text.render_line(0).text.rstrip().endswith("…")
            for text in welcome._tips.query(Static):
                assert text.region in app.screen.region
                assert text.region.right <= bounds.right
                assert text.content_size.width >= text.render().cell_length
