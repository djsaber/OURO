"""TUI application scenarios: interactions."""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import Static

from ouro.sdk.plugins.interactions import PlanConfirmationRequest
from ouro.tui.app import OuroTuiApp
from ouro.tui.composer.input import Composer
from ouro.tui.interactions.bar import InteractionBar
from ouro.tui.interactions.plan import PlanConfirmationPanel
from ouro.tui.interactions.questions import AskUserPanel
from ouro.tui.state import PendingInteraction
from ouro.tui.widgets.activity import ActivityIndicator
from ouro.tui.widgets.brand import BrandHeader
from tests.tui.app.support import (
    _application,
    _approval_application,
    _multi_approval_application,
    _plan_confirmation_application,
    _question_application,
    _wait_for_tool_approval,
)
from tests.tui.helpers import run_ready_app, wait_for_observed_status
from tests.tui.helpers import wait_for_worker_group as _wait_for_worker_group
from tests.tui.helpers import wait_until as _wait_until


@pytest.mark.asyncio
async def test_switching_sessions_clears_the_previous_approval_panel(tmp_path: Path) -> None:
    application = _application(tmp_path)
    destination = application.runtime.sessions.create(
        session_id="ses_destination",
        title="Destination",
    ).session
    app = OuroTuiApp(application=application)

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        stale = PendingInteraction(
            kind="approval",
            run_id="run_previous",
            tool_call_id="call_previous",
            tool_name="Write",
            request_id="approval_previous",
            session_id="ses_previous",
        )
        app._interaction = stale
        app._sync_interaction(stale)
        assert app.query_one(InteractionBar).display is True

        await app._open_session(destination.session_id)
        await pilot.pause()

        assert app._projection is not None
        assert app._projection.session_id == destination.session_id
        assert app._active_title == "Destination"
        assert str(tmp_path)[:20] in app.query_one(BrandHeader).render().plain
        assert app._interaction is None
        assert app.query_one(InteractionBar).display is False


@pytest.mark.asyncio
async def test_approval_panel_selects_with_arrows_and_resumes_run(tmp_path: Path) -> None:
    target = tmp_path / "approved.txt"
    app = OuroTuiApp(
        application=_approval_application(tmp_path, target),
        approval_mode="approval",
    )

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        await pilot.click("#composer")
        await pilot.press("w", "r", "i", "t", "e", "enter")
        panel = await _wait_for_tool_approval(app, pilot, tool_call_id="call_write")

        assert app._projection is not None
        assert app._projection.pending_interaction is not None
        assert app._projection.pending_interaction.kind == "approval"
        composer = app.query_one(Composer)
        assert panel.has_focus is True
        assert panel.selected_approved is True
        assert composer.disabled is True
        title = panel.query_one("#interaction-title", Static).render().plain
        controls = panel.query_one("#interaction-controls", Static).render().plain
        assert title == "APPROVAL"
        assert "1. Allow" not in controls
        assert "2. Deny" not in controls
        assert "Y/N direct" not in controls
        assert "↑/↓ move • Enter choose" in controls
        assert "←/→" not in controls

        await pilot.press("n")
        await pilot.pause()
        assert app._projection.pending_interaction is not None
        assert panel.selected_approved is True

        await pilot.press("left", "right")
        await pilot.pause()
        assert panel.selected_approved is True

        await pilot.press("down")
        assert panel.selected_approved is False

        await pilot.press("up")
        assert panel.selected_approved is True

        await pilot.press("enter")
        await _wait_for_worker_group(app, pilot, group="interaction")
        await wait_for_observed_status(pilot, status="completed")

        assert target.read_text(encoding="utf-8") == "written"
        assert app._projection.pending_interaction is None
        assert app._projection.activity.status == "completed"
        assert composer.disabled is False


@pytest.mark.asyncio
async def test_approval_panel_keeps_selected_action_visible_in_short_terminal(
    tmp_path: Path,
) -> None:
    target = tmp_path / "approved.txt"
    app = OuroTuiApp(
        application=_approval_application(tmp_path, target),
        approval_mode="approval",
    )

    async with run_ready_app(app, size=(40, 12)) as pilot:
        await pilot.pause()
        await pilot.click("#composer")
        await pilot.press(*"write", "enter")
        panel = await _wait_for_tool_approval(app, pilot, tool_call_id="call_write")

        assert panel.region.intersection(app.screen.region)

        await pilot.press("down")
        await pilot.pause()
        assert panel.selected_approved is False
        assert "Deny" in app.export_screenshot()
        await pilot.press("enter")
        await _wait_for_worker_group(app, pilot, group="interaction")
        await wait_for_observed_status(pilot, status="completed")
        assert not panel.display
        assert not target.exists()


@pytest.mark.asyncio
async def test_parallel_approvals_advance_one_at_a_time_without_stalling(tmp_path: Path) -> None:
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    app = OuroTuiApp(
        application=_multi_approval_application(tmp_path, (first, second)),
        approval_mode="approval",
    )

    async with run_ready_app(app, size=(100, 36)) as pilot:
        await pilot.pause()
        await pilot.click("#composer")
        await pilot.press("r", "u", "n", "enter")
        await _wait_for_tool_approval(app, pilot, tool_call_id="call_write_1", pending_count=2)

        assert app._projection is not None
        assert [
            interaction.tool_call_id for interaction in app._projection.pending_interactions
        ] == ["call_write_1", "call_write_2"]
        title = app.query_one("#interaction-title", Static)
        assert title.render().plain == "APPROVAL  1/2"

        await pilot.press("enter")
        await _wait_for_tool_approval(app, pilot, tool_call_id="call_write_2")

        assert app._projection.pending_interaction is not None
        assert app._projection.pending_interaction.tool_call_id == "call_write_2"
        assert app.query_one(InteractionBar).has_focus is True
        assert title.render().plain == "APPROVAL  2/2"
        assert first.exists() is False
        assert second.exists() is False

        await pilot.press("enter")
        await _wait_for_worker_group(app, pilot, group="interaction")
        await wait_for_observed_status(pilot, status="completed")

        assert first.read_text(encoding="utf-8") == "first"
        assert second.read_text(encoding="utf-8") == "second"
        assert app._projection.pending_interaction is None
        assert app._projection.activity.status == "completed"


@pytest.mark.asyncio
async def test_approval_panel_denies_selected_tool(tmp_path: Path) -> None:
    target = tmp_path / "denied.txt"
    app = OuroTuiApp(
        application=_approval_application(tmp_path, target),
        approval_mode="approval",
    )

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        await pilot.click("#composer")
        await pilot.press("w", "r", "i", "t", "e", "enter")
        panel = await _wait_for_tool_approval(app, pilot, tool_call_id="call_write")

        await pilot.press("down", "enter")
        # Projection state can change before asynchronous interaction rendering.
        await _wait_until(
            lambda: (
                app._projection is not None
                and app._projection.pending_interaction is None
                and not panel.display
            ),
            pilot,
            description="denied approval resolution",
        )

        assert target.exists() is False
        assert app._projection is not None
        assert app._projection.pending_interaction is None
        assert panel.display is False


@pytest.mark.asyncio
async def test_builtin_ask_user_renders_form_and_submits_structured_answer(
    tmp_path: Path,
) -> None:
    application = _question_application(tmp_path)
    app = OuroTuiApp(application=application)

    async with run_ready_app(app, size=(100, 38)) as pilot:
        await pilot.pause()
        await pilot.click("#composer")
        await pilot.press("a", "s", "k", "enter")
        await _wait_until(
            lambda: app.query_one(AskUserPanel).display,
            pilot,
            description="question form",
        )

        panel = app.query_one(AskUserPanel)
        assert panel.display is True
        assert panel.has_focus is True
        rendered_question = panel.render().plain
        assert rendered_question.splitlines()[0] == "FILE  1/1"
        assert "Which file should OURO use?" in rendered_question
        assert "↑/↓ move • Enter choose" in rendered_question
        assert "←/→" not in rendered_question

        await pilot.press("left", "right")
        await pilot.pause()
        assert panel.render().plain == rendered_question

        await pilot.press("2")
        await _wait_until(
            lambda: (
                app._projection is not None
                and app._projection.pending_interaction is None
                and app._projection.assistant_text == "Answer received."
            ),
            pilot,
            description="structured question answer",
        )
        await _wait_for_worker_group(app, pilot, group="interaction")

        assert app._projection is not None
        assert app._projection.pending_interaction is None
        assert app._projection.assistant_text == "Answer received."
        assert panel.display is False
        events = application.runtime.sessions.event_page(
            app._projection.session_id,
            cursor=0,
            limit=200,
        ).events
        resolved = next(event for event in events if event.event_type == "ask_user_resolved")
        assert resolved.payload["answer"] == {"answers": {"file": ["README.md"]}}


@pytest.mark.asyncio
async def test_ask_user_other_accepts_freeform_even_when_answer_starts_with_slash(
    tmp_path: Path,
) -> None:
    application = _question_application(tmp_path)
    app = OuroTuiApp(application=application)

    async with run_ready_app(app, size=(100, 38)) as pilot:
        await pilot.pause()
        await pilot.click("#composer")
        await pilot.press("a", "s", "k", "enter")
        await _wait_until(
            lambda: app.query_one(AskUserPanel).display,
            pilot,
            description="question form",
        )

        await pilot.press("down", "down", "enter")
        await pilot.pause()
        panel = app.query_one(AskUserPanel)
        assert panel.awaiting_freeform is True
        assert app.query_one(Composer).has_focus is True

        await pilot.press("slash", "t", "m", "p", "enter")
        await _wait_until(
            lambda: app._projection is not None and app._projection.pending_interaction is None,
            pilot,
            description="freeform question answer",
        )
        await _wait_for_worker_group(app, pilot, group="interaction")

        assert app._projection is not None
        assert app._projection.pending_interaction is None
        events = application.runtime.sessions.event_page(
            app._projection.session_id,
            cursor=0,
            limit=200,
        ).events
        resolved = next(event for event in events if event.event_type == "ask_user_resolved")
        assert resolved.payload["answer"] == {"answers": {"file": ["/tmp"]}}


@pytest.mark.asyncio
async def test_plugin_question_uses_common_form_and_returns_scalar_answer(
    tmp_path: Path,
) -> None:
    application = _question_application(tmp_path, tool_name="PluginQuestion")
    app = OuroTuiApp(application=application)

    async with run_ready_app(app, size=(100, 38)) as pilot:
        await pilot.pause()
        await pilot.click("#composer")
        await pilot.press("a", "s", "k", "enter")
        await _wait_until(
            lambda: app.query_one(AskUserPanel).display,
            pilot,
            description="plugin question form",
        )

        panel = app.query_one(AskUserPanel)
        assert panel.display is True
        assert "How should the plugin continue?" in str(panel.render())

        await pilot.press("1")
        await _wait_until(
            lambda: app._projection is not None and app._projection.pending_interaction is None,
            pilot,
            description="plugin question answer",
        )
        await _wait_for_worker_group(app, pilot, group="interaction")

        assert app._projection is not None
        assert app._projection.pending_interaction is None
        events = application.runtime.sessions.event_page(
            app._projection.session_id,
            cursor=0,
            limit=200,
        ).events
        resolved = next(event for event in events if event.event_type == "ask_user_resolved")
        assert resolved.payload["answer"] == "Safely"


@pytest.mark.asyncio
async def test_plan_mode_write_approval_keeps_focus_on_approval_panel(tmp_path: Path) -> None:
    application = _plan_confirmation_application(tmp_path)
    await application.start()
    assert application.plugins is not None
    assert (await application.plugins.enable("dev.ouro.plan")).status == "active"
    app = OuroTuiApp(application=application, approval_mode="approval")

    async with run_ready_app(app, size=(100, 42)) as pilot:
        await pilot.pause()
        await pilot.press("slash", *"plan", "space", "o", "n", "enter")
        await _wait_for_worker_group(app, pilot, group="plugin-command")
        await _wait_until(
            lambda: app._projection is not None and not app.query_one(Composer).disabled,
            pilot,
            description="plan session ready for input",
        )
        await pilot.click("#composer")
        await pilot.press("p", "l", "a", "n", "enter")

        panel = app.query_one(InteractionBar)
        await _wait_until(
            lambda: panel.display and panel.has_focus,
            pilot,
            description="plan file write approval",
        )
        composer = app.query_one(Composer)
        assert composer.disabled is True
        assert panel.has_focus is True

        await pilot.click("#composer")
        await pilot.pause()
        assert composer.has_focus is False
        assert panel.has_focus is True

        await pilot.press("tab")
        await pilot.pause()

        assert composer.has_focus is False
        assert panel.has_focus is True


@pytest.mark.asyncio
async def test_plan_confirmation_arrows_page_document_without_moving_options(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))
    request = PlanConfirmationRequest(
        plan_path=str(tmp_path / "plan.md"),
        plan="# Long plan\n\n" + "\n".join(f"{index}. Verify step {index}." for index in range(30)),
        plan_digest="digest",
    )

    async with run_ready_app(app, size=(60, 24)) as pilot:
        await pilot.pause()
        panel = app.query_one(PlanConfirmationPanel)
        panel.show_request(request)
        await pilot.pause()

        document = panel.query_one("#plan-confirmation-document")
        assert document.max_scroll_y > 0
        assert panel.selected_action == "accept"

        await pilot.press("right")
        await pilot.pause()
        assert document.scroll_y > 0
        assert panel.selected_action == "accept"

        await pilot.press("left")
        await pilot.pause()
        assert document.scroll_y == 0
        assert panel.selected_action == "accept"

        await pilot.press("pagedown")
        await pilot.pause()
        assert document.scroll_y > 0
        await pilot.press("pageup")
        await pilot.pause()
        assert document.scroll_y == 0

        await pilot.press("down")
        assert panel.selected_action == "revise"


@pytest.mark.asyncio
async def test_plan_confirmation_renders_only_plan_and_collects_revision_feedback(
    tmp_path: Path,
) -> None:
    application = _plan_confirmation_application(tmp_path)
    await application.start()
    assert application.plugins is not None
    assert (await application.plugins.enable("dev.ouro.plan")).status == "active"
    app = OuroTuiApp(application=application, approval_mode="auto")

    async with run_ready_app(app, size=(100, 42)) as pilot:
        await pilot.pause()
        await pilot.press("slash", *"plan", "space", "o", "n", "enter")
        await _wait_for_worker_group(app, pilot, group="plugin-command")
        await _wait_until(
            lambda: app._projection is not None and not app.query_one(Composer).disabled,
            pilot,
            description="plan session ready for input",
        )
        await pilot.click("#composer")
        await pilot.press("p", "l", "a", "n", "enter")

        panel = app.query_one(PlanConfirmationPanel)
        await _wait_until(
            lambda: panel.display and panel.has_focus,
            pilot,
            description="plan confirmation",
        )
        ask_user = app.query_one(AskUserPanel)
        plan_body = panel.query_one("#plan-confirmation-body", Static).render().plain
        controls = panel.query_one("#plan-confirmation-controls", Static).render().plain

        assert panel.display is True
        assert panel.has_focus is True
        assert ask_user.display is False
        assert plan_body == "# Release plan\n\n1. Inspect.\n2. Ship safely.\n3. Verify."
        assert "C:/private/session/plan.md" not in plan_body
        assert "plan_digest" not in plan_body
        assert '"actions"' not in plan_body
        assert "Accept" in controls
        assert "Revise" in controls
        assert "Reject" in controls
        assert panel.selected_action == "accept"

        await pilot.press("down", "enter")
        await pilot.pause()

        composer = app.query_one(Composer)
        assert panel.awaiting_feedback is True
        assert composer.has_focus is True
        assert composer.placeholder == Composer.PLAN_FEEDBACK_PLACEHOLDER

        await pilot.press(*"Add rollback checks.", "enter")
        await _wait_until(
            lambda: app._projection is not None and app._projection.pending_interaction is None,
            pilot,
            description="plan revision answer",
        )
        await _wait_for_worker_group(app, pilot, group="interaction")

        assert app._projection is not None
        assert app._projection.pending_interaction is None
        assert panel.display is False
        events = application.runtime.sessions.event_page(
            app._projection.session_id,
            cursor=0,
            limit=200,
        ).events
        requested = next(event for event in events if event.event_type == "ask_user_requested")
        resolved = next(event for event in events if event.event_type == "ask_user_resolved")
        assert requested.payload["canonical_tool_id"] == "dev.ouro.plan/tools/exit"
        assert resolved.payload["answer"] == {
            "action": "revise",
            "feedback": "Add rollback checks.",
        }


@pytest.mark.asyncio
async def test_plan_confirmation_accepts_a_keyboard_selected_implementation_option(
    tmp_path: Path,
) -> None:
    application = _plan_confirmation_application(
        tmp_path,
        options=[
            {"label": "Safe rollout", "description": "Keep rollback available."},
            {"label": "Direct rollout", "description": "Complete the change at once."},
        ],
    )
    await application.start()
    assert application.plugins is not None
    assert (await application.plugins.enable("dev.ouro.plan")).status == "active"
    app = OuroTuiApp(application=application, approval_mode="auto")

    async with run_ready_app(app, size=(100, 42)) as pilot:
        await pilot.pause()
        await pilot.press("slash", *"plan", "space", "o", "n", "enter")
        await _wait_for_worker_group(app, pilot, group="plugin-command")
        await _wait_until(
            lambda: app._projection is not None and not app.query_one(Composer).disabled,
            pilot,
            description="plan session ready for input",
        )
        await pilot.click("#composer")
        await pilot.press("p", "l", "a", "n", "enter")

        panel = app.query_one(PlanConfirmationPanel)
        await _wait_until(
            lambda: panel.display and panel.has_focus,
            pilot,
            description="plan confirmation",
        )
        await pilot.press("enter")
        await pilot.pause()

        controls = panel.query_one("#plan-confirmation-controls", Static).render().plain
        assert "Choose an implementation approach" in controls
        assert "Safe rollout" in controls
        assert "Direct rollout" in controls

        await pilot.press("2")
        await pilot.pause()
        assert app._projection is not None
        assert app._projection.pending_interaction is not None

        await pilot.press("down", "enter")
        await _wait_until(
            lambda: app._projection is not None and app._projection.pending_interaction is None,
            pilot,
            description="selected plan implementation",
        )
        await _wait_for_worker_group(app, pilot, group="interaction")

        assert app._projection is not None
        await _wait_until(
            lambda: "Plan" not in app.query_one(ActivityIndicator).render().plain,
            pilot,
            description="released plan-mode status",
        )
        events = application.runtime.sessions.event_page(
            app._projection.session_id,
            cursor=0,
            limit=200,
        ).events
        resolved = next(event for event in events if event.event_type == "ask_user_resolved")
        assert resolved.payload["answer"] == {
            "action": "accept",
            "option": "Direct rollout",
        }
