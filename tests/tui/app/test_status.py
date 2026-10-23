"""TUI application scenarios: status."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from textual.widgets import Input, OptionList, Static

import ouro.tui.app as tui_app_module
import ouro.tui.conversation.history_rendering as conversation_history
import ouro.tui.conversation.reasoning as tui_widgets_reasoning
import ouro.tui.conversation.tools as tui_widgets_tools
from ouro.application.contracts.events import project_runtime_event
from ouro.application.contracts.execution import RuntimeActivitySnapshot, RuntimeToolCallActivity
from ouro.application.contracts.models import AvailableModel, ModelReasoningState
from ouro.application.contracts.sessions import SessionContextStatus
from ouro.application.observations.sessions import observation_event
from ouro.core.context import ContextMetrics
from ouro.core.context_budget import ContextBudget
from ouro.core.events import EventRecord
from ouro.core.reasoning import ReasoningCapability, ReasoningRequest
from ouro.tui.app import OuroTuiApp
from ouro.tui.composer.input import Composer
from ouro.tui.composer.popup import CommandPopup
from ouro.tui.conversation.history import ConversationHistory
from ouro.tui.conversation.view import ConversationView
from ouro.tui.menus.choice import ChoiceScreen
from ouro.tui.state import TuiSessionProjection
from ouro.tui.subagent_navigation import SubagentNavigationPanel
from ouro.tui.widgets.activity import ActivityIndicator
from ouro.tui.widgets.status import StatusLine
from tests.tui.app.providers import RetryThenBlockProvider
from tests.tui.app.support import (
    _application,
    _blocking_application,
    _endpoint_named_application,
    _thinking_application,
    _wait_for_active_session,
)
from tests.tui.helpers import command_popup_text, run_ready_app
from tests.tui.helpers import wait_for_screen as _wait_for_screen
from tests.tui.helpers import wait_for_worker_group as _wait_for_worker_group
from tests.tui.helpers import wait_until as _wait_until


@pytest.mark.asyncio
async def test_tui_context_status_matches_latest_prepared_context_size(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        status_line = app.query_one(StatusLine)
        context_status = SessionContextStatus(
            context_id="ctx_1",
            session_id="session_1",
            run_id="run_1",
            turn_id="turn_1",
            model_round=1,
            provider_id="anthropic",
            model_id="claude-test",
            prepared_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
            metrics=ContextMetrics(
                estimated_input_tokens=48000,
                estimated_fixed_tokens=1000,
                budget=ContextBudget(context_window=128000, max_output_tokens=8000),
            ),
            # Provider usage can exclude cached input and therefore is not
            # the current prepared context size shown by /context.
            provider_reported_input_tokens=800,
        )
        status_line.set_context(context_status)

        context_detail = status_line.query_one("#context-detail", Static)
        context = context_detail.render()
        assert context.plain == "48.0K █████▒▒▒▒▒▒ 40%"

        at_warning_boundary = ContextMetrics(
            estimated_input_tokens=96000,
            estimated_fixed_tokens=1000,
            budget=ContextBudget(context_window=128000, max_output_tokens=8000),
        )
        status_line.set_context(context_status.model_copy(update={"metrics": at_warning_boundary}))
        boundary = context_detail.render()
        assert boundary.plain == "96.0K █████████▒▒ 80%"

        above_warning_boundary = ContextMetrics(
            estimated_input_tokens=97200,
            estimated_fixed_tokens=1000,
            budget=ContextBudget(context_window=128000, max_output_tokens=8000),
        )
        status_line.set_context(
            context_status.model_copy(update={"metrics": above_warning_boundary})
        )
        warning = context_detail.render()
        assert warning.plain == "97.2K █████████▒▒ 81%"

        status_line.set_context(None)
        assert context_detail.render().plain == ""


@pytest.mark.asyncio
async def test_status_line_shows_model_retry_attempts_after_the_first_attempt(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        first_attempt = RuntimeActivitySnapshot(
            session_id="ses_retry",
            run_id="run_retry",
            status="running",
            activity="waiting_model",
        )
        app._sync_activity(first_attempt)
        indicator = app.query_one(ActivityIndicator)

        assert "Waiting for model •" in indicator.render().plain
        assert "/5" not in indicator.render().plain

        app._sync_activity(
            first_attempt.model_copy(update={"model_attempt": 2, "model_max_attempts": 5})
        )
        assert "Waiting for model 2/5" in indicator.render().plain

        app._sync_activity(
            first_attempt.model_copy(update={"model_attempt": 3, "model_max_attempts": 5})
        )
        assert "Waiting for model 3/5" in indicator.render().plain


@pytest.mark.asyncio
async def test_repeated_runtime_activity_skips_unchanged_presentations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        session_id = "session_hot_path"
        activity = RuntimeActivitySnapshot(
            session_id=session_id,
            run_id="run_hot_path",
            status="running",
            activity="streaming_text",
        )
        projection = TuiSessionProjection(session_id)
        projection.activity = activity
        app._projection = projection
        app._controller._session_id = session_id
        app._sync_activity(activity)
        indicator = app.query_one(ActivityIndicator)
        assert indicator._timer is not None
        indicator._timer.stop()
        indicator._timer = None

        def reject_redundant_render() -> None:
            raise AssertionError("unchanged runtime activity triggered presentation work")

        monkeypatch.setattr(
            indicator,
            "_render_indicator",
            reject_redundant_render,
        )
        monkeypatch.setattr(
            app.query_one(StatusLine),
            "_render_context",
            reject_redundant_render,
        )
        monkeypatch.setattr(
            app.query_one(SubagentNavigationPanel),
            "_refresh_content",
            reject_redundant_render,
        )
        monkeypatch.setattr(
            app.query_one(Composer),
            "_render_styled_placeholder",
            lambda _parts: reject_redundant_render(),
        )

        app._sync_activity(activity)


@pytest.mark.asyncio
async def test_session_animations_only_layout_when_geometry_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        conversation = app.query_one(ConversationView)
        indicator = app.query_one(ActivityIndicator)
        indicator_layouts: list[bool] = []

        def record_indicator_refresh(*_regions: object, **kwargs: object) -> ActivityIndicator:
            indicator_layouts.append(bool(kwargs.get("layout", False)))
            return indicator

        monkeypatch.setattr(indicator, "refresh", record_indicator_refresh)
        indicator._tick()
        assert indicator_layouts == [False]

        await conversation.append_reasoning("Inspecting the render path")
        reasoning = conversation._assistant
        assert reasoning is not None
        if reasoning.reasoning._timer is not None:
            reasoning.reasoning._timer.stop()
            reasoning.reasoning._timer = None
        label = reasoning.reasoning.label
        label_layouts: list[bool] = []

        def record_label_refresh(
            *_regions: object, **kwargs: object
        ) -> tui_widgets_reasoning.ReasoningLabel:
            label_layouts.append(bool(kwargs.get("layout", False)))
            return label

        monkeypatch.setattr(label, "refresh", record_label_refresh)
        label.set_content(
            conversation_history._reasoning_label_text("Thinking", 1.1, expanded=True)
        )
        label_layouts.clear()
        label.set_content(
            conversation_history._reasoning_label_text("Thinking", 1.2, expanded=True)
        )
        assert label_layouts == [False]

        label_layouts.clear()
        label.set_content(
            conversation_history._reasoning_label_text("Thinking", 10.0, expanded=True)
        )
        assert label_layouts == [True]

        await conversation.finish_assistant()
        tool = RuntimeToolCallActivity(
            tool_call_id="call_animation_layout",
            tool_name="Read",
            status="running",
            source_index=0,
        )
        await conversation.sync_tools((tool,))
        row = conversation.query_one(tui_widgets_tools.ToolActivityRow)
        if row._timer is not None:
            row._timer.stop()
            row._timer = None
        tool_layouts: list[bool] = []

        def record_tool_refresh(
            *_regions: object, **kwargs: object
        ) -> tui_widgets_tools.ToolActivityRow:
            tool_layouts.append(bool(kwargs.get("layout", False)))
            return row

        monkeypatch.setattr(row, "refresh", record_tool_refresh)
        row._tick()
        assert tool_layouts == [False]


@pytest.mark.asyncio
async def test_context_detail_only_layouts_when_its_cell_width_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        status_line = app.query_one(StatusLine)
        status_line.set_context_metrics(None)
        detail = status_line.query_one("#context-detail", Static)
        layouts: list[bool] = []

        def record_refresh(*_regions: object, **kwargs: object) -> Static:
            layouts.append(bool(kwargs.get("layout", False)))
            return detail

        monkeypatch.setattr(detail, "refresh", record_refresh)
        metrics = ContextMetrics(
            estimated_input_tokens=48000,
            estimated_fixed_tokens=1000,
            budget=ContextBudget(context_window=128000, max_output_tokens=8000),
        )
        status_line.set_context_metrics(metrics)
        status_line.set_context_metrics(
            metrics.model_copy(update={"estimated_input_tokens": 49_200})
        )
        status_line.set_context_metrics(None)

        assert layouts == [True, False, True]


@pytest.mark.asyncio
async def test_status_line_updates_while_a_model_retry_is_waiting(tmp_path: Path) -> None:
    provider = RetryThenBlockProvider()
    app = OuroTuiApp(application=_blocking_application(tmp_path, provider))

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        try:
            await pilot.press(*"retry", "enter")
            await _wait_until(
                lambda: "Waiting for model 3/5" in app.query_one(ActivityIndicator).render().plain,
                pilot,
                description="model retry status",
            )

            assert "Waiting for model 3/5" in app.query_one(ActivityIndicator).render().plain
        finally:
            provider.release.set()
        await _wait_until(
            lambda: "Ready" in app.query_one(ActivityIndicator).render().plain,
            pilot,
            description="ready status after model retry",
        )
        assert "Ready" in app.query_one(ActivityIndicator).render().plain


@pytest.mark.asyncio
async def test_status_line_shows_the_model_display_name(tmp_path: Path) -> None:
    application = _endpoint_named_application(tmp_path)
    session = application.runtime.create_session(title="Endpoint status")
    app = OuroTuiApp(
        application=application,
        resume_session_id=session.session_id,
    )

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        indicator = app.query_one(StatusLine).query_one(ActivityIndicator)

        assert "Ready • inherit • Friendly model" in indicator.render().plain
        assert "supplier/" not in indicator.render().plain
        assert "provider-model-id" not in indicator.render().plain
        assert "chat-endpoint" not in indicator.render().plain

        await app._select_model("supplier/chat-endpoint")

        assert "Ready • inherit • Friendly model" in indicator.render().plain


@pytest.mark.asyncio
async def test_status_line_projects_arbitrary_session_control_label_first(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        status_line = app.query_one(StatusLine)
        indicator = status_line.query_one(ActivityIndicator)

        status_line.set_session_controls(("Review Harness",))
        assert indicator.render().plain.startswith("▣ Review Harness • Ready")

        status_line.set_session_controls(())
        assert indicator.render().plain.startswith("▣ Ready")
        assert "Review Harness" not in indicator.render().plain


@pytest.mark.asyncio
async def test_model_switch_keeps_context_usage_and_updates_model_capacity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = _application(tmp_path)
    result = await application.runtime.run("create context")
    app = OuroTuiApp(
        application=application,
        resume_session_id=result.session.session_id,
    )

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await _wait_for_active_session(app, pilot, result.session.session_id)
        await _wait_until(
            lambda: (
                app._projection is not None
                and app._projection.session_id == result.session.session_id
            ),
            pilot,
            description="restored session projection",
        )
        projection = app._projection
        assert projection is not None
        before = projection.activity.effective_context_metrics
        assert before is not None
        selected = app._models[0].model_copy(
            update={"context_window": 256_000, "max_output_tokens": 16_000}
        )

        async def select_model(_selector: str) -> AvailableModel:
            return selected

        monkeypatch.setattr(app._controller, "select_model", select_model)
        await app._select_model(selected.key)

        after = projection.activity.effective_context_metrics
        assert after is not None
        assert after.estimated_input_tokens == before.estimated_input_tokens
        assert after.estimated_fixed_tokens == before.estimated_fixed_tokens
        assert after.estimator_id == before.estimator_id
        assert after.context_window_tokens == 256_000
        assert after.reserved_output_tokens == 16_000
        assert after.input_budget_tokens == 240_000
        status_line = app.query_one(StatusLine)
        assert status_line._context_metrics == after
        assert status_line.query_one("#context-detail", Static).render().plain

        app._sync_activity(projection.activity)
        assert status_line._context_metrics == after


@pytest.mark.asyncio
async def test_live_host_event_updates_context_without_reloading(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_application(tmp_path))
    metrics = ContextMetrics(
        estimated_input_tokens=250,
        estimated_fixed_tokens=100,
        budget=ContextBudget(context_window=1000, max_output_tokens=100),
    )

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        session_id = "ses_live_context"
        app._projection = TuiSessionProjection(session_id)
        started = EventRecord(
            session_id=session_id,
            run_id="run_live_context",
            turn_id="turn_live_context",
            event_type="model_request_started",
            sequence=1,
            payload={
                "context_id": "ctx_live",
                "provider": "test",
                "endpoint": "primary",
                "model": "test-model",
                "round": 1,
                "turn_id": "turn_live_context",
                "context_metrics": metrics.model_dump(mode="json"),
            },
        )

        await app._apply_runtime_events((observation_event(project_runtime_event(started)),))
        await pilot.pause()

        assert app.query_one("#context-detail", Static).render().plain.startswith("250 ")


@pytest.mark.parametrize(
    ("capability", "effective", "expected"),
    [
        (ReasoningCapability(), ReasoningRequest(), ()),
        (
            ReasoningCapability(
                activation="toggle",
                default=ReasoningRequest(mode="enabled"),
            ),
            ReasoningRequest(mode="enabled"),
            ("enabled", "disabled"),
        ),
        (
            ReasoningCapability(
                activation="toggle",
                supported_efforts=("low", "high"),
                default=ReasoningRequest(mode="enabled", effort="low"),
            ),
            ReasoningRequest(mode="enabled", effort="low"),
            ("disabled", "low", "high"),
        ),
        (
            ReasoningCapability(
                activation="always_on",
                supported_efforts=("low", "high"),
                default=ReasoningRequest(mode="enabled", effort="low"),
            ),
            ReasoningRequest(mode="enabled", effort="low"),
            ("low", "high"),
        ),
        (
            ReasoningCapability(
                activation="always_on",
                default=ReasoningRequest(mode="enabled"),
            ),
            ReasoningRequest(mode="enabled"),
            (),
        ),
    ],
)
def test_thinking_choices_expose_only_distinct_user_configurable_effects(
    capability: ReasoningCapability,
    effective: ReasoningRequest,
    expected: tuple[str, ...],
) -> None:
    state = ModelReasoningState(
        model=AvailableModel(
            key="test/model",
            provider_id="test",
            endpoint_id="model",
            model_id="model",
            reasoning=capability,
        ),
        capability=capability,
        requested=ReasoningRequest(),
        effective=effective,
    )

    arguments = tui_app_module._reasoning_arguments(state)

    assert tuple(argument.value for argument in arguments) == expected
    labels = {"enabled": "On", "disabled": "Off", "low": "Low", "high": "High"}
    assert tuple(argument.display_label for argument in arguments) == tuple(
        labels[value] for value in expected
    )


@pytest.mark.asyncio
async def test_thinking_completion_and_modal_share_supported_choices(
    tmp_path: Path,
) -> None:
    application = _thinking_application(tmp_path)
    application.runtime.adapter.model.reasoning.supported_summaries = ("auto", "detailed")
    app = OuroTuiApp(application=application)

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        await pilot.press("slash", *"thinking", "space")
        await pilot.pause()

        inline = command_popup_text(app.query_one(CommandPopup)).plain
        for value in ("Off", "Low", "High", "Summary: Auto", "Summary: Detailed"):
            assert value in inline
        for value in ("enabled", "minimal", "medium", "xhigh", "max", "Concise", "Summarized"):
            assert value not in inline

        app.query_one(Composer).clear()
        app.action_thinking_settings()
        await pilot.pause()
        modal = {choice.value for choice in app.screen._choices}
        for value in ("disabled", "low", "high", "summary:auto", "summary:detailed"):
            assert value in modal
        for value in ("default", "enabled", "minimal", "medium", "xhigh", "max", "summary:concise"):
            assert value not in modal
        options = app.screen.query_one("#choice-list", OptionList)
        options.highlighted = options.get_option_index("summary:detailed")
        await pilot.press("enter")
        await pilot.pause()
        await app._set_reasoning("high")
        state = await app._controller.session_reasoning()
        assert state.requested == ReasoningRequest(
            mode="enabled", effort="high", summary="detailed"
        )
        await app._set_reasoning("summary:provider_default")
        state = await app._controller.session_reasoning()
        assert state.requested == ReasoningRequest(
            mode="enabled", effort="high", summary="provider_default"
        )


@pytest.mark.asyncio
async def test_thinking_modal_remains_available_during_an_active_run(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_thinking_application(tmp_path))

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        await pilot.press(*"start", "enter")
        await _wait_until(
            lambda: app._projection is not None and app._projection.activity.status == "completed",
            pilot,
            description="completed initial run",
        )
        assert app._projection is not None
        app._projection.activity = app._projection.activity.model_copy(update={"status": "running"})

        app.action_thinking_settings()
        await _wait_for_screen(app, ChoiceScreen, title="MODEL THINKING")
        app.screen.query_one("#choice-filter", Input).value = "high"
        await pilot.press("enter")
        await pilot.pause()

        reasoning = await app._controller.session_reasoning()
        assert reasoning.requested.effort == "high"


@pytest.mark.asyncio
async def test_status_line_shows_thinking_level_and_highlights_peak_effort(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_thinking_application(tmp_path))

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        indicator = app.query_one(ActivityIndicator)

        # The endpoint default effort is low — not the highest supported — so
        # the level is shown without the peak highlight.
        assert indicator._reasoning == "low"
        assert indicator._reasoning_peak is False
        assert "low" in indicator.render().plain

        # Raising to high (the highest effort this endpoint supports) peaks the
        # indicator so the level is highlighted.
        await pilot.press("slash", *"thinking", "enter", *"high", "enter")
        await pilot.pause()
        assert indicator._reasoning == "high"
        assert indicator._reasoning_peak is True
        assert "high" in indicator.render().plain


@pytest.mark.asyncio
async def test_status_line_hides_thinking_level_when_reasoning_unsupported(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        indicator = app.query_one(ActivityIndicator)
        # The default faux endpoint declares no reasoning capability, so no
        # thinking level is projected onto the status line.
        assert indicator._reasoning is None


@pytest.mark.asyncio
async def test_status_line_hides_thinking_when_disabled(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_thinking_application(tmp_path))

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        indicator = app.query_one(ActivityIndicator)
        active_separator_count = indicator.render().plain.count(" • ")

        await pilot.press("slash", *"thinking", "enter", *"disabled", "enter")
        await pilot.pause()
        assert indicator._reasoning is None
        # Disabling reasoning removes the trailing separator so the status line
        # shows only the activity, approval mode, and model endpoint.
        assert indicator.render().plain.count(" • ") == active_separator_count - 1


@pytest.mark.asyncio
async def test_plan_command_without_dialogue_keeps_welcome_and_updates_status_line(
    tmp_path: Path,
) -> None:
    application = _application(tmp_path)
    await application.start()
    assert application.plugins is not None
    enabled = await application.plugins.enable("dev.ouro.plan")
    assert enabled.status == "active"
    app = OuroTuiApp(application=application)

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        await pilot.press("slash", "p", "l", "a", "n", "space", "o", "n", "enter")
        await _wait_for_worker_group(app, pilot, group="plugin-command")
        await _wait_until(
            lambda: "Plan" in app.query_one(ActivityIndicator).render().plain,
            pilot,
            description="active plan-mode status",
        )

        await _wait_until(
            lambda: app._projection is not None,
            pilot,
            description="empty session observation",
        )
        conversation = app.query_one(ConversationView)
        indicator = app.query_one(ActivityIndicator)
        assert app._projection.session_id == app._controller.session_id
        assert conversation.showing_welcome is True
        assert conversation.query_one(ConversationHistory).entries == ()
        assert app._has_active_run() is False
        assert app.query_one(Composer).disabled is False
        assert indicator.render().plain.startswith("▣ Plan • Ready")

        await pilot.press("slash", "p", "l", "a", "n", "space", "o", "f", "f", "enter")
        await _wait_for_worker_group(app, pilot, group="plugin-command")
        await _wait_until(
            lambda: "Plan" not in indicator.render().plain,
            pilot,
            description="released plan-mode status",
        )

        assert app._projection.session_id == app._controller.session_id
        assert conversation.showing_welcome is True
        assert conversation.query_one(ConversationHistory).entries == ()
        assert indicator.render().plain.startswith("▣ Ready")
        assert "Plan" not in indicator.render().plain


@pytest.mark.asyncio
async def test_loaded_session_projects_active_harness_into_status_line(tmp_path: Path) -> None:
    application = _application(tmp_path)
    await application.start()
    assert application.plugins is not None
    assert (await application.plugins.enable("dev.ouro.plan")).status == "active"
    session = application.runtime.create_session(title="Active harness")
    await application.plugins.execute_command(
        "dev.ouro.plan:plan",
        "on",
        session_id=session.session_id,
        workspace=str(tmp_path),
    )
    app = OuroTuiApp(application=application, resume_session_id=session.session_id)

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await _wait_for_active_session(app, pilot, session.session_id)

        indicator = app.query_one(ActivityIndicator)
        await _wait_until(
            lambda: "Plan" in indicator.render().plain,
            pilot,
            description="restored harness status",
        )
        assert indicator.render().plain.startswith("▣ Plan • Ready")

        await pilot.press("slash", *"plan off", "enter")
        await _wait_for_worker_group(app, pilot, group="plugin-command")
        await _wait_until(
            lambda: "Plan" not in indicator.render().plain,
            pilot,
            description="released harness status",
        )

        assert "Plan" not in indicator.render().plain
        notices = tuple(
            entry.text
            for entry in app.query_one(ConversationHistory).entries
            if entry.kind == "notice"
        )
        assert not any("Plan mode ended" in notice for notice in notices)
