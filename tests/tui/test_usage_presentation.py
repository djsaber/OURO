from __future__ import annotations

import pytest
from textual.widgets import Static

from ouro.application.usage_models import UsageSummary, UsageSummaryGroup, UsageTotals
from ouro.core.billing import UsageFilter
from ouro.tui.app import OuroTuiApp
from ouro.tui.controller import TuiSystemStatus, project_usage_system_status
from ouro.tui.widgets import WelcomePanel
from tests.tui.helpers import run_ready_app, two_route_application


@pytest.mark.parametrize(
    "count,priced,cost,modes",
    [
        (0, 0, 0, ()),
        (1, 0, 0, ("subscription",)),
        (1, 1, 0, ("usage_based",)),
        (1, 1, 0.1, ("usage_based",)),
        (2, 1, 0.1, ("subscription", "usage_based")),
    ],
)
@pytest.mark.parametrize("width", [80, 120])
async def test_welcome_shows_usage_without_cost_or_billing_information(
    tmp_path,
    count,
    priced,
    cost,
    modes,
    width,
):
    totals = UsageTotals(
        request_count=count,
        priced_request_count=priced,
        unpriced_request_count=count - priced,
        estimated_cost=cost,
        billing_modes=modes,
        input_tokens=30,
        output_tokens=4,
        cache_read_tokens=10,
    )
    summary = UsageSummary(usage_filter=UsageFilter(), totals=totals)
    status = project_usage_system_status(TuiSystemStatus(), summary)
    app = OuroTuiApp(application=two_route_application(tmp_path))
    async with run_ready_app(app, size=(width, 40)) as pilot:
        block = app.query_one(WelcomePanel)._usage
        block.set_system_status(status)
        await pilot.pause()
        assert block._requests.render().plain == f"{count} request(s)"
        assert block._requests.content_size.width >= block._requests.render().cell_length
        assert [widget.render().plain for widget in block.query(Static)] == [
            "USAGE",
            block._chart.render().plain,
            f"{count} request(s)",
            "30 in • 4 out • 33.33% cache",
        ]


async def test_hovered_day_shows_request_count_without_cost_or_billing_information(tmp_path):
    summary = UsageSummary(
        usage_filter=UsageFilter(),
        group_by="date",
        totals=UsageTotals(
            request_count=2,
            priced_request_count=1,
            unpriced_request_count=1,
            estimated_cost=0.1,
            billing_modes=("subscription", "usage_based"),
        ),
        groups=(
            UsageSummaryGroup(
                key="2026-09-16",
                totals=UsageTotals(
                    request_count=1, unpriced_request_count=1, billing_modes=("subscription",)
                ),
            ),
            UsageSummaryGroup(
                key="2026-09-17",
                totals=UsageTotals(
                    request_count=1,
                    priced_request_count=1,
                    estimated_cost=0.1,
                    billing_modes=("usage_based",),
                ),
            ),
        ),
    )
    app = OuroTuiApp(application=two_route_application(tmp_path))
    async with run_ready_app(app, size=(120, 40)) as pilot:
        block = app.query_one(WelcomePanel)._usage
        block.set_system_status(project_usage_system_status(TuiSystemStatus(), summary))
        await pilot.pause()
        await pilot.hover(block._chart, offset=(0, 0))
        await pilot.pause()
        assert block._requests.render().plain == "1 request(s)"
        await pilot.hover(block._chart, offset=(1, 0))
        await pilot.pause()
        assert block._requests.render().plain == "1 request(s)"
        assert [widget.render().plain for widget in block.query(Static)] == [
            "USAGE",
            block._chart.render().plain,
            "1 request(s)",
            "0 in • 0 out • 0.00% cache",
        ]
