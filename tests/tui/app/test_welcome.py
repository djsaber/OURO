"""TUI application scenarios: welcome."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ouro.skills import SkillMetadata
from ouro.tui.app import OuroTuiApp
from ouro.tui.composer.input import Composer
from ouro.tui.composer.surface import WelcomePanel
from ouro.tui.conversation.history import ConversationHistory
from ouro.tui.conversation.view import ConversationView
from ouro.tui.view_models import TuiBootstrap, TuiDailyUsage, TuiSystemStatus
from ouro.tui.widgets.brand import BrandHeader
from ouro.tui.widgets.status import StatusLine
from tests.tui.app.providers import BlockingStreamProvider
from tests.tui.app.support import _application, _blocking_application, _wait_for_welcome_render
from tests.tui.helpers import run_ready_app
from tests.tui.helpers import wait_for_worker_group as _wait_for_worker_group
from tests.tui.helpers import wait_until as _wait_until


@pytest.mark.asyncio
async def test_tui_hides_welcome_header_while_startup_is_still_in_progress(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))
    startup_entered = asyncio.Event()
    release_startup = asyncio.Event()
    start = app._controller.start

    async def delayed_start() -> TuiBootstrap:
        startup_entered.set()
        await release_startup.wait()
        return await start()

    app._controller.start = delayed_start  # type: ignore[method-assign]

    async with app.run_test(size=(100, 32)) as pilot:
        try:
            await asyncio.wait_for(startup_entered.wait(), timeout=1)
            await pilot.pause()
            header = app.query_one(BrandHeader)
            composer = app.query_one(Composer)
            status_line = app.query_one(StatusLine)
            assert not header.display
            assert not composer.display
            assert status_line.display
            assert status_line.region.bottom == app.screen.region.bottom
            visible_input_children = [
                child
                for child in app.query_one(WelcomePanel).input_surface.children
                if child.display
            ]
            assert visible_input_children == [status_line]
            assert not app.query_one(WelcomePanel).page.display
        finally:
            release_startup.set()


@pytest.mark.asyncio
async def test_welcome_refreshes_live_system_status_each_time_it_opens(tmp_path: Path) -> None:
    application = _application(tmp_path)
    app = OuroTuiApp(application=application)

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        live_skill = SkillMetadata(
            name="live-skill",
            description="Added after TUI startup.",
            location=str(tmp_path / "live-skill" / "SKILL.md"),
        )
        await application.refresh_skills((live_skill,))
        assert app._system_status.skill_names == ()

        await app._show_welcome()
        welcome = await _wait_for_welcome_render(app, pilot, contains="live-skill")

        assert app._system_status.skill_names == ("live-skill",)
        assert "1 available • live-skill" in welcome.page._system._skills.render().plain


@pytest.mark.asyncio
async def test_welcome_usage_hover_selects_day_without_changing_color_and_previews_totals(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))
    status = TuiSystemStatus(
        usage_request_count=3,
        usage_input_tokens=2_000,
        usage_output_tokens=500,
        usage_cache_read_tokens=500,
        usage_daily=(
            TuiDailyUsage(
                date="2026-09-01",
                request_count=1,
                input_tokens=800,
                output_tokens=200,
                cache_read_tokens=100,
            ),
            TuiDailyUsage(
                date="2026-09-02",
                request_count=2,
                input_tokens=1_200,
                output_tokens=300,
                cache_read_tokens=400,
            ),
        ),
    )

    async with run_ready_app(app, size=(100, 32)) as pilot:
        welcome = await _wait_for_welcome_render(app, pilot, contains="Commands")
        usage = welcome.page._usage
        usage.set_system_status(status)
        await pilot.pause()

        chart = usage._chart.render()
        assert chart.plain == "■■ • last 2 days"
        assert usage._requests.render().plain == "3 request(s)"
        assert usage._tokens.render().plain == "2.0K in • 500 out • 25.00% cache"

        await pilot.hover(usage._chart, offset=(0, 0))
        await pilot.pause()

        hovered_chart = usage._chart.render()
        assert hovered_chart.plain == "▣■ • 2026-09-01"
        assert usage._requests.render().plain == "1 request(s)"
        assert usage._tokens.render().plain == "800 in • 200 out • 12.50% cache"

        await pilot.hover(None, offset=(0, 31))
        await pilot.pause()

        assert usage._chart.render().plain == "■■ • last 2 days"
        assert usage._tokens.render().plain == "2.0K in • 500 out • 25.00% cache"


def test_welcome_usage_cache_hit_rate_is_zero_without_input_tokens() -> None:
    from ouro.tui.welcome.components import _cache_hit_rate

    assert _cache_hit_rate(100, 0) == 0.0


@pytest.mark.asyncio
async def test_first_user_message_renders_when_leaving_welcome_page(tmp_path: Path) -> None:
    provider = BlockingStreamProvider()
    app = OuroTuiApp(application=_blocking_application(tmp_path, provider))

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await _wait_until(
            lambda: app.query_one(ConversationView).showing_welcome,
            pilot,
            description="welcome page",
        )

        try:
            await pilot.press(*"first message", "enter")
            await _wait_until(
                lambda: app._projection is not None and app._projection.assistant_text == "first ",
                pilot,
                description="first streamed response chunk",
            )

            history = app.query_one(ConversationHistory)
            assert history.user_turns == ("first message",)
            assert "first message" in history.rendered_text
            assert not app.query_one(WelcomePanel).page.display
        finally:
            provider.release.set()
        await _wait_for_worker_group(app, pilot, group="run")

        assert app._projection is not None
        assert app._projection.activity.status == "completed"
