from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from ouro.tui.app import OuroTuiApp
from ouro.tui.composer.input import Composer
from ouro.tui.subagent_navigation import SubagentNavigationPanel
from ouro.tui.widgets.activity import ActivityIndicator
from tests.tui.app.providers import MultiChildStreamingProvider
from tests.tui.app.support import (
    _application,
    _multi_child_streaming_application,
    _persist_direct_child_session,
)
from tests.tui.helpers import (
    release_on_exit,
    run_ready_app,
    wait_for_events,
    wait_for_worker_group,
    wait_until,
)


@pytest.mark.parametrize("finish_at_child", [False, True])
async def test_rapid_agent_navigation_loads_only_settled_target(
    tmp_path, monkeypatch, finish_at_child
):
    application = _application(tmp_path)
    root_id, child_id = _persist_direct_child_session(application)
    app = OuroTuiApp(application=application)
    async with run_ready_app(app) as pilot:
        await app._open_session(root_id)
        observe = AsyncMock(wraps=app._controller.observe_session)
        monkeypatch.setattr(app._controller, "observe_session", observe)
        run_worker = app.run_worker
        switches = []

        def capture_worker(work, **kwargs):
            worker = run_worker(work, **kwargs)
            if work == app._switch_agent_nodes:
                switches.append(worker)
            return worker

        monkeypatch.setattr(app, "run_worker", capture_worker)
        panel = app.query_one(SubagentNavigationPanel)
        for _ in range(10):
            app.action_agent_node_right()
            assert panel.selected_session_id == child_id
            await asyncio.sleep(0)
            app.action_agent_node_root()
            assert panel.selected_session_id == root_id
            await asyncio.sleep(0)
        if finish_at_child:
            app.action_agent_node_right()
        assert observe.await_count == 0
        await wait_for_worker_group(app, pilot, group="navigation")
        assert len(switches) == 1
        assert observe.await_count == int(finish_at_child)
        assert app._visible_session_id() == (child_id if finish_at_child else root_id)
        assert not panel.navigation_active


async def test_repeated_target_does_not_restart_inflight_agent_load(tmp_path, monkeypatch):
    application = _application(tmp_path)
    root_id, child_id = _persist_direct_child_session(application)
    app = OuroTuiApp(application=application)
    entered = asyncio.Event()
    release = asyncio.Event()
    async with run_ready_app(app) as pilot:
        await app._open_session(root_id)
        observe = app._controller.observe_session
        reads = []

        async def delayed_observe(session_id, **kwargs):
            reads.append(session_id)
            entered.set()
            await release.wait()
            return await observe(session_id, **kwargs)

        monkeypatch.setattr(app._controller, "observe_session", delayed_observe)
        try:
            app.action_agent_node_right()
            await wait_for_events(entered, pilot=pilot, description="inflight child load")
            revision = app._view_revision
            for _ in range(10):
                app.action_agent_node_right()
            assert app._view_revision == revision
        finally:
            release.set()
            await wait_for_worker_group(app, pilot, group="navigation")
        assert reads == [child_id]
        assert app._visible_session_id() == child_id


@pytest.mark.parametrize("yield_before_cancel", [False, True])
async def test_menu_navigation_cancels_pending_switch_and_allows_next_switch(
    tmp_path, monkeypatch, yield_before_cancel
):
    application = _application(tmp_path)
    root_id, child_id = _persist_direct_child_session(application)
    app = OuroTuiApp(application=application)
    async with run_ready_app(app) as pilot:
        await app._open_session(root_id)
        observe = AsyncMock(wraps=app._controller.observe_session)
        monkeypatch.setattr(app._controller, "observe_session", observe)
        app.action_agent_node_right()
        if yield_before_cancel:
            for _ in range(5):
                await asyncio.sleep(0)
        app.action_sessions()
        await wait_for_worker_group(app, pilot, group="navigation")
        assert app._visible_session_id() == root_id
        assert observe.await_count == 0
        panel = app.query_one(SubagentNavigationPanel)
        assert panel.selected_session_id == root_id
        assert not panel.navigation_active
        await pilot.press("escape")
        app.action_agent_node_right()
        await wait_for_worker_group(app, pilot, group="navigation")
        assert app._visible_session_id() == child_id
        assert observe.await_count == 1


async def test_return_to_visible_session_discards_inflight_load_without_reloading(
    tmp_path, monkeypatch
):
    application = _application(tmp_path)
    root_id, child_id = _persist_direct_child_session(application)
    app = OuroTuiApp(application=application)
    entered = asyncio.Event()
    release = asyncio.Event()
    async with run_ready_app(app) as pilot:
        await app._open_session(root_id)
        observe = app._controller.observe_session
        observations = []

        async def delayed_observe(session_id, **kwargs):
            observation = await observe(session_id, **kwargs)
            observations.append(observation)
            entered.set()
            await release.wait()
            return observation

        monkeypatch.setattr(app._controller, "observe_session", delayed_observe)
        try:
            app.action_agent_node_right()
            await wait_for_events(entered, pilot=pilot, description="prepared child load")
            app.action_agent_node_root()
        finally:
            release.set()
            await wait_for_worker_group(app, pilot, group="navigation")
        assert len(observations) == 1
        assert observations[0].loaded.detail.session.session_id == child_id
        assert observations[0].subscription.closed
        assert app._visible_session_id() == root_id
        assert app.query_one(ActivityIndicator)._activity != "loading_session"


@pytest.mark.parametrize("destination", ["menu", "welcome", "session"])
async def test_cancelled_child_load_preserves_final_surface_and_draft(
    tmp_path, monkeypatch, destination
):
    application = _application(tmp_path)
    root_id, child_id = _persist_direct_child_session(application)
    other = application.runtime.create_session(title="Final session")
    app = OuroTuiApp(application=application)
    entered = asyncio.Event()
    release = asyncio.Event()
    subscriptions = []
    async with run_ready_app(app) as pilot:
        await app._open_session(root_id)
        composer = app.query_one(Composer)
        composer.load_text("Root draft")
        await pilot.pause()
        app._remember_input_draft(None, "Welcome draft")
        app._remember_input_draft(other.session_id, "Final draft")
        install = app._install_session_surface

        async def delayed_install(loaded, **kwargs):
            if loaded.detail.session.session_id == child_id:
                subscriptions.append(kwargs["subscription"])
                entered.set()
                await release.wait()
            return await install(loaded, **kwargs)

        monkeypatch.setattr(app, "_install_session_surface", delayed_install)
        try:
            app.action_agent_node_right()
            await wait_for_events(entered, pilot=pilot, description="prepared child before cancel")
            if destination == "welcome":
                app.action_new_session()
            else:
                app.action_sessions()
            await wait_for_worker_group(app, pilot, group="navigation")
            if destination == "session":
                await pilot.press("escape")
                await app._open_session(other.session_id)
        finally:
            release.set()
            await wait_for_worker_group(app, pilot, group="navigation")
        expected_id = {"menu": root_id, "welcome": None, "session": other.session_id}[destination]
        expected_draft = {
            "menu": "Root draft",
            "welcome": "Welcome draft",
            "session": "Final draft",
        }[destination]
        assert subscriptions and all(subscription.closed for subscription in subscriptions)
        assert app._visible_session_id() == expected_id
        assert app._controller.session_id == expected_id
        assert composer.text == expected_draft
        assert not composer.disabled
        assert app.query_one(ActivityIndicator)._activity != "loading_session"
        if destination == "menu":
            await pilot.press("escape")
        await app._open_session(root_id)
        assert composer.text == "Root draft"


async def test_late_child_load_cannot_replace_latest_target_or_interrupt_runs_and_draft(
    tmp_path, monkeypatch
):
    provider = MultiChildStreamingProvider()
    application = _multi_child_streaming_application(tmp_path, provider)
    app = OuroTuiApp(application=application)
    entered = asyncio.Event()
    release = asyncio.Event()
    async with (
        run_ready_app(app) as pilot,
        release_on_exit(release, provider.release_root, *provider.releases.values()),
    ):
        composer = app.query_one(Composer)
        composer.load_text("Start children")
        await pilot.press("enter")
        await wait_for_events(
            provider.root_stream_started,
            *provider.first_chunks.values(),
            pilot=pilot,
            description="root and child streams",
        )
        root_id = app._controller.session_id
        await wait_until(
            lambda: len(app._agent_nodes) == 2,
            pilot,
            description="both navigation nodes",
        )
        panel = app.query_one(SubagentNavigationPanel)
        first_id, second_id = panel.visible_session_ids
        composer.load_text("Original root draft")
        await pilot.pause()
        install = app._install_session_surface
        committed = []
        stale_subscriptions = []

        async def delayed_install(loaded, **kwargs):
            session_id = loaded.detail.session.session_id
            if session_id == first_id:
                stale_subscriptions.append(kwargs["subscription"])
                entered.set()
                await release.wait()
            installed = await install(loaded, **kwargs)
            if installed:
                committed.append(session_id)
            return installed

        monkeypatch.setattr(app, "_install_session_surface", delayed_install)
        app.action_agent_node_right()
        await wait_for_events(entered, pilot=pilot, description="stale first child load")
        composer.load_text("Edited while switching")
        await pilot.pause()
        for _ in range(5):
            app.action_agent_node_last()
            app.action_agent_node_root()
        app.action_agent_node_last()
        assert app._visible_session_id() == root_id
        assert not composer.disabled
        release.set()
        await wait_for_worker_group(app, pilot, group="navigation")
        assert committed == [second_id]
        assert stale_subscriptions and all(item.closed for item in stale_subscriptions)
        assert app._visible_session_id() == second_id
        assert app._controller.session_id == root_id
        assert composer.disabled
        runs = {
            session_id: application.runtime.store.load_session_record(session_id).active_run_id
            for session_id in (root_id, first_id, second_id)
        }
        assert all(runs.values())
        provider.release_root.set()
        for child_release in provider.releases.values():
            child_release.set()
        await wait_until(
            lambda: all(
                application.runtime.store.load_run(session_id, run_id).status == "completed"
                for session_id, run_id in runs.items()
            ),
            pilot,
            description="all Agent runs complete after rapid navigation",
        )
        assert app._visible_session_id() == second_id
        app.action_agent_node_root()
        await wait_for_worker_group(app, pilot, group="navigation")
        assert app._visible_session_id() == root_id
        assert composer.text == "Edited while switching"
        assert not composer.disabled
        assert app._input_drafts.draft_for(root_id) == "Edited while switching"
