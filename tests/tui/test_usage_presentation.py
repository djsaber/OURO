from __future__ import annotations

import pytest

from ouro.application.usage_models import UsageSummary, UsageSummaryGroup, UsageTotals
from ouro.core.billing import UsageFilter
from ouro.tui.app import OuroTuiApp
from ouro.tui.controller import TuiSystemStatus, project_usage_system_status
from ouro.tui.widgets import WelcomePanel
from tests.tui.helpers import run_ready_app, two_route_application


@pytest.mark.parametrize(
    "count,priced,cost,modes,expected",
    [
        (0, 0, 0, (), "0 request(s)"),
        (1, 0, 0, ("subscription",), "1 request(s) • cost unknown • 1 unpriced"),
        (1, 1, 0, ("usage_based",), "1 request(s) • $0.0000"),
        (1, 1, 0.1, ("usage_based",), "1 request(s) • $0.1000"),
        (
            2,
            1,
            0.1,
            ("subscription", "usage_based"),
            "2 request(s) • $0.1000 (partial) • 1 unpriced",
        ),
    ],
)
@pytest.mark.parametrize("width", [80, 120])
async def test_welcome_distinguishes_unknown_partial_zero_and_empty_cost(
    tmp_path,
    count,
    priced,
    cost,
    modes,
    expected,
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
        assert block._requests.render().plain == expected
        assert block._requests.content_size.width >= block._requests.render().cell_length
        assert block._billing.display == bool(modes)
        if modes:
            assert "Billing: " in block._billing.render().plain
        assert "30 in" in block._tokens.render().plain
        assert "4 out" in block._tokens.render().plain


async def test_hovered_day_uses_its_own_billing_modes_and_unknown_count(tmp_path):
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
        assert block._requests.render().plain == "1 request(s) • cost unknown • 1 unpriced"
        assert block._billing.render().plain == "Billing: subscription"
        await pilot.hover(block._chart, offset=(1, 0))
        await pilot.pause()
        assert block._requests.render().plain == "1 request(s) • $0.1000"
        assert block._billing.render().plain == "Billing: usage-based"
