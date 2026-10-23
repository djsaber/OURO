"""TUI application scenarios: agents."""

from __future__ import annotations

from pathlib import Path

import pytest
from textual import events
from textual.app import App
from textual.content import Content
from textual.widgets import Input, OptionList

from ouro.application.contracts.agents import AgentToolOptionView
from ouro.core.agents import ToolSelection
from ouro.core.messages import MessageRecord, TextContent
from ouro.tui.app import OuroTuiApp
from ouro.tui.composer.input import Composer
from ouro.tui.composer.popup import CommandPopup
from ouro.tui.conversation.history import ConversationHistory
from ouro.tui.conversation.messages import ToolNavigationRequested
from ouro.tui.conversation.turn import AssistantTurn
from ouro.tui.conversation.view import ConversationView
from ouro.tui.features.configuration.agents import (
    AgentDefinitionConfigurationScreen,
    AgentDispatchPolicyScreen,
)
from ouro.tui.features.configuration.compaction import CompactionConfigurationScreen
from ouro.tui.menus.choice import ChoiceScreen
from ouro.tui.menus.confirmation import ConfirmationScreen
from ouro.tui.menus.editable import EditableChoiceScreen
from ouro.tui.menus.models import Choice
from ouro.tui.preferences import TuiPreferences, TuiPreferencesStore
from ouro.tui.subagent_navigation import SubagentNavigationPanel
from ouro.tui.theme import DEFAULT_TUI_THEME
from ouro.tui.widgets.activity import ActivityIndicator
from ouro.tui.widgets.brand import BrandHeader
from tests.tui.app.providers import MultiChildStreamingProvider
from tests.tui.app.support import (
    _application,
    _multi_child_streaming_application,
    _persist_direct_child_session,
)
from tests.tui.helpers import release_on_exit, run_ready_app, wait_for_events, wait_for_menu_action
from tests.tui.helpers import wait_for_screen as _wait_for_screen
from tests.tui.helpers import wait_for_worker_group as _wait_for_worker_group
from tests.tui.helpers import wait_until as _wait_until


@pytest.mark.asyncio
async def test_choice_page_refreshes_in_place_after_a_child_operation() -> None:
    app = App()
    label = "Before"

    async def refresh_choices() -> tuple[Choice, ...]:
        return (Choice("item", label),)

    async def update_item(_selected: str) -> tuple[Choice, ...] | None:
        nonlocal label
        label = "After"
        return None

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.push_screen(
            ChoiceScreen(
                title="ITEMS",
                placeholder="Filter items…",
                choices=(Choice("item", label),),
                on_select=update_item,
                on_refresh=refresh_choices,
            )
        )
        await pilot.pause()
        options = app.screen.query_one(OptionList)

        await pilot.press("enter")
        await pilot.pause()

        assert app.screen.query_one(OptionList) is options
        assert app.screen._choices == (Choice("item", "After"),)


@pytest.mark.asyncio
@pytest.mark.parametrize("collapse_traces", [True, False])
async def test_tui_switches_between_root_and_multiple_read_only_child_streams(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    collapse_traces: bool,
) -> None:
    TuiPreferencesStore(tmp_path).save(TuiPreferences(collapse_traces=collapse_traces))
    provider = MultiChildStreamingProvider()
    application = _multi_child_streaming_application(tmp_path, provider)
    app = OuroTuiApp(home=tmp_path, application=application)

    async with (
        run_ready_app(app, size=(100, 38)) as pilot,
        release_on_exit(provider.release_root, *provider.releases.values()),
    ):
        await pilot.pause()
        await pilot.click("#composer")
        await pilot.press(*"start two children", "enter")
        await wait_for_events(
            provider.root_stream_started,
            *provider.first_chunks.values(),
            pilot=pilot,
            description="root and both child streams started",
        )
        root_session_id = app._controller.session_id
        assert root_session_id is not None
        assert application.agents is not None
        tree = application.agents.agent_tree_snapshot(root_session_id)
        by_call = {node.parent_tool_call_id: node for node in tree.nodes}
        first = by_call["spawn_child_1"]
        second = by_call["spawn_child_2"]
        root_history = app.query_one(ConversationHistory)

        def child_entry(call_id: str):
            return next(
                (
                    entry
                    for entry in root_history.entries
                    if entry.tool is not None and entry.tool.tool_call_id == call_id
                ),
                None,
            )

        await _wait_until(
            lambda: all(
                (entry := child_entry(call_id)) is not None
                and entry.agent is not None
                and entry.agent.activity is not None
                and entry.agent.activity.activity == "streaming_text"
                for call_id in ("spawn_child_1", "spawn_child_2")
            ),
            pilot,
            description="root subagent previews",
        )
        for index, call_id in enumerate(("spawn_child_1", "spawn_child_2"), start=1):
            entry = child_entry(call_id)
            assert entry is not None
            assert entry.agent is not None
            assert entry.agent.task_description == f"Observe child {index}"
            assert entry.agent.agent_name in root_history.rendered_text
        assert "SpawnAgent" not in root_history.rendered_text
        assert "└ Responding • Observe child 1" in root_history.rendered_text
        assert "└ Responding • Observe child 2" in root_history.rendered_text
        subagent_panel = app.query_one(SubagentNavigationPanel)
        assert subagent_panel.display is True
        assert subagent_panel.current_session_id == root_session_id
        assert subagent_panel.visible_session_ids == (first.session_id, second.session_id)
        await pilot.press("f1")
        await _wait_for_screen(app, ChoiceScreen, title="KEYBOARD HELP")
        app.post_message(events.Key("alt+right", None))
        await pilot.pause()
        assert app._visible_session_id() == root_session_id
        await pilot.press("escape")
        await pilot.click("#composer")
        await pilot.press("slash")
        await _wait_until(
            lambda: app.query_one(CommandPopup).display,
            pilot,
            description="command popup above composer",
        )
        command_popup = app.query_one(CommandPopup)
        assert subagent_panel.region.bottom == command_popup.region.y
        assert command_popup.region.bottom == app.query_one(Composer).region.y
        await pilot.press("escape")
        await _wait_until(
            lambda: not command_popup.display,
            pilot,
            description="dismissed command popup",
        )
        app.query_one(Composer).load_text("pending root draft")
        root_conversation = app.query_one(ConversationView)
        await pilot.pause()
        assert root_conversation.max_scroll_y > 0
        root_conversation.scroll_to(
            y=root_conversation.max_scroll_y - 2,
            animate=False,
            immediate=True,
        )
        await pilot.pause()
        held_root_scroll_y = root_conversation.scroll_y
        assert held_root_scroll_y > 3
        assert held_root_scroll_y < root_conversation.max_scroll_y

        app.post_message(
            ToolNavigationRequested(
                parent_run_id=first.parent_run_id,
                parent_tool_call_id="spawn_child_1",
            )
        )
        await _wait_until(
            lambda: app._visible_session_id() == first.session_id,
            pilot,
            description="first child view",
        )
        await _wait_for_worker_group(app, pilot, group="navigation")
        assert app._controller.session_id == root_session_id
        assert app._visible_read_only is True
        assert app.query_one(Composer).disabled is True
        assert app._projection is not None
        assert app._projection.assistant_text == provider.child_prefixes["first child task"]
        child_history = app.query_one(ConversationHistory)
        frozen_text = "".join(
            entry.text for entry in child_history.entries if entry.kind == "assistant"
        )
        assert (
            frozen_text + app.query_one(AssistantTurn).source
            == provider.child_prefixes["first child task"]
        )
        child_header = app.query_one(BrandHeader).render()
        assert "read-only" not in child_header.plain
        assert app.query_one(BrandHeader)._session_detail == first.task_description
        child_status = app.query_one(ActivityIndicator).render().plain
        assert "Subagent" not in child_status
        assert "Responding" in child_status
        assert "read-only" not in child_status
        await app._refresh_session_controls()
        assert "Subagent" not in app.query_one(ActivityIndicator).render().plain
        child_placeholder = app.query_one(Composer).placeholder
        assert isinstance(child_placeholder, Content)
        assert child_placeholder.plain == (
            "Subagent view is read-only • Alt+↑ main • Alt+←/→ switch"
        )
        composer = app.query_one(Composer)
        assert composer.has_class("read-only-session")
        assert composer.text == ""
        assert app._controlled_composer_draft == "pending root draft"
        assert any(
            entry.kind == "user" and entry.text == "first child task"
            for entry in app.query_one(ConversationHistory).entries
        )

        writes: list[str] = []

        async def reject_submit(prompt: str, **_kwargs):
            writes.append(f"submit:{prompt}")
            raise AssertionError("read-only child view submitted input")

        async def reject_abort():
            writes.append("abort")
            raise AssertionError("read-only child view aborted a Run")

        monkeypatch.setattr(app._controller, "submit", reject_submit)
        monkeypatch.setattr(app._controller, "abort", reject_abort)
        app.post_message(Composer.Submitted("must not submit", "follow_up"))
        app.action_abort()
        await pilot.pause()
        assert writes == []

        provider.release_root.set()
        await _wait_for_worker_group(app, pilot, group="run")
        assert app._visible_session_id() == first.session_id
        assert app._visible_read_only is True
        assert app.query_one(Composer).disabled is True

        provider.releases["first child task"].set()
        await _wait_until(
            lambda: app._projection is not None and app._projection.activity.status == "completed",
            pilot,
            description="first child stream completion",
        )
        await _wait_until(
            lambda: "Done" in app.query_one(ActivityIndicator).render().plain,
            pilot,
            description="first child task result",
        )
        child_history = app.query_one(ConversationHistory)
        # Cache reuse and held scroll offsets require the terminal transcript,
        # not only the completion status supplied by live activity updates.
        await _wait_until(
            lambda: bool(child_history.entries) and child_history.entries[-1].kind == "run",
            pilot,
            description="first child completion summary",
        )
        await child_history.wait_until_rendered()
        await pilot._wait_for_screen()
        assert "Subagent" not in app.query_one(ActivityIndicator).render().plain
        first_child_text = "\n".join(
            entry.text for entry in child_history.entries if entry.kind == "assistant"
        )
        assert "first child paragraph 0" in first_child_text
        assert " + first child suffix" in first_child_text

        child_conversation = app.query_one(ConversationView)
        assert child_conversation.max_scroll_y > 3
        assert held_root_scroll_y > child_conversation.max_scroll_y
        child_conversation.scroll_to(
            y=child_conversation.max_scroll_y - 2,
            animate=False,
            immediate=True,
        )
        await pilot.pause()
        held_child_scroll_y = child_conversation.scroll_y
        assert held_child_scroll_y < child_conversation.max_scroll_y

        app.action_main_session()
        await _wait_for_worker_group(app, pilot, group="navigation")
        await root_history.wait_until_rendered()
        await app.screen.wait_for_refresh()
        assert app._visible_session_id() == root_session_id
        # Restore within the new content bounds. Folding does not guarantee a
        # shorter document: the final answer also arrived while this view was hidden.
        restored_root_scroll_y = min(held_root_scroll_y, root_conversation.max_scroll_y)
        assert root_conversation.scroll_y == restored_root_scroll_y
        assert app._visible_read_only is False
        composer = app.query_one(Composer)
        assert composer.disabled is False
        assert not composer.has_class("read-only-session")
        assert composer.text == "pending root draft"
        assert app._controlled_composer_draft is None
        assert "Root history paragraph 0" in "\n".join(
            entry.text
            for entry in app.query_one(ConversationHistory).entries
            if entry.kind == "assistant"
        )
        assert " + root suffix" in "\n".join(
            entry.text
            for entry in app.query_one(ConversationHistory).entries
            if entry.kind == "assistant"
        )
        root_history = app.query_one(ConversationHistory)
        await _wait_until(
            lambda: (
                (entry := child_entry("spawn_child_1")) is not None
                and entry.agent is not None
                and entry.agent.status == "completed"
            ),
            pilot,
            description="completed first child preview",
        )
        assert ("└ Done • Observe child 1" in root_history.rendered_text) is (not collapse_traces)

        rendered_during_cached_switch: list[int] = []
        write_entry = ConversationHistory._write_entry

        def capture_cached_switch_render(
            history: ConversationHistory,
            *args,
            entry_index: int,
            **kwargs,
        ) -> None:
            # Root previews remain live while the child snapshot is loading.
            # This assertion measures reuse of the destination child history.
            if history.entries and history.entries[0].text == "first child task":
                rendered_during_cached_switch.append(entry_index)
            write_entry(history, *args, entry_index=entry_index, **kwargs)

        monkeypatch.setattr(
            ConversationHistory,
            "_write_entry",
            capture_cached_switch_render,
        )

        app.post_message(
            ToolNavigationRequested(
                parent_run_id=first.parent_run_id,
                parent_tool_call_id="spawn_child_1",
            )
        )
        await _wait_until(
            lambda: app._visible_session_id() == first.session_id,
            pilot,
            description="reopened first child view",
        )
        await _wait_for_worker_group(app, pilot, group="navigation")
        assert app.query_one(ConversationView).scroll_y == held_child_scroll_y
        # Live observation already rendered and cached the completed child's
        # summary, so reopening needs no additional history rendering.
        assert app.query_one(ConversationHistory).entries[-1].kind == "run"
        assert rendered_during_cached_switch == []
        rendered_during_cached_switch.clear()
        app.post_message(events.Key("alt+right", None))
        await _wait_until(
            lambda: app._visible_session_id() == second.session_id,
            pilot,
            description="direct sibling child view",
        )
        await _wait_for_worker_group(app, pilot, group="navigation")
        assert app._controller.session_id == root_session_id
        assert subagent_panel.current_session_id == second.session_id
        assert "Observe child 2" in subagent_panel.render_content(80).plain
        rendered_during_cached_switch.clear()

        app.post_message(events.Key("alt+up", None))
        await _wait_until(
            lambda: app._visible_session_id() == root_session_id,
            pilot,
            description="return from sibling child view",
        )
        await _wait_for_worker_group(app, pilot, group="navigation")
        assert rendered_during_cached_switch == []

        app.post_message(
            ToolNavigationRequested(
                parent_run_id=second.parent_run_id,
                parent_tool_call_id="spawn_child_2",
            )
        )
        await _wait_until(
            lambda: app._visible_session_id() == second.session_id,
            pilot,
            description="second child view",
        )
        await _wait_for_worker_group(app, pilot, group="navigation")
        assert app._controller.session_id == root_session_id
        assert app._projection is not None
        assert app._projection.assistant_text == provider.child_prefixes["second child task"]

        app.action_main_session()
        await _wait_until(
            lambda: app._visible_session_id() == root_session_id,
            pilot,
            description="leave second child while it is streaming",
        )
        await _wait_for_worker_group(app, pilot, group="navigation")
        provider.releases["second child task"].set()
        await _wait_until(
            lambda: (
                (node := app._agent_nodes.get(second.delegation_id)) is not None
                and node.status == "completed"
            ),
            pilot,
            description="invisible second child completion",
        )
        app.post_message(
            ToolNavigationRequested(
                parent_run_id=second.parent_run_id,
                parent_tool_call_id="spawn_child_2",
            )
        )
        await _wait_until(
            lambda: (
                app._visible_session_id() == second.session_id
                and app._projection is not None
                and app._projection.activity.status == "completed"
            ),
            pilot,
            description="reloaded completed second child",
        )
        await _wait_for_worker_group(app, pilot, group="navigation")
        second_child_text = "\n".join(
            entry.text
            for entry in app.query_one(ConversationHistory).entries
            if entry.kind == "assistant"
        )
        assert "second child paragraph 0" in second_child_text
        assert " + second child suffix" in second_child_text


@pytest.mark.asyncio
async def test_child_history_presents_only_its_own_delegated_user_inputs(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))
    delegated = MessageRecord(
        session_id="ses_child",
        role="user",
        content=[TextContent(text="Inspect the delegated target")],
        scaffold=True,
        metadata={
            "session_input_source": "delegation",
            "delegation_id": "dlg_child",
        },
    )
    unrelated = delegated.model_copy(
        update={
            "message_id": "msg_unrelated_delegation",
            "content": [TextContent(text="Unrelated delegated task")],
            "metadata": {
                "session_input_source": "delegation",
                "delegation_id": "dlg_other",
            },
        }
    )
    follow_up = delegated.model_copy(
        update={
            "message_id": "msg_delegated_follow_up",
            "content": [TextContent(text="Check one more file")],
        }
    )

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        conversation = app.query_one(ConversationView)

        await conversation.load_messages((delegated, unrelated))
        assert conversation.query_one(ConversationHistory).entries == ()

        await conversation.load_messages(
            (delegated, unrelated),
            visible_scaffold_delegation_id="dlg_child",
        )
        await conversation.add_user_message(unrelated)
        await conversation.add_user_message(follow_up)

        entries = conversation.query_one(ConversationHistory).entries
        assert [(entry.kind, entry.text) for entry in entries] == [
            ("user", "Inspect the delegated target"),
            ("user", "Check one more file"),
        ]
        assert [entry.created_at for entry in entries] == [
            delegated.created_at,
            follow_up.created_at,
        ]


@pytest.mark.asyncio
async def test_session_picker_groups_child_and_agent_tree_keeps_it_navigable(
    tmp_path: Path,
) -> None:
    application = _application(tmp_path)
    root_session_id, child_session_id = _persist_direct_child_session(application)
    app = OuroTuiApp(application=application)

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await _wait_until(
            lambda: app._controller._started,
            pilot,
            description="TUI startup with persisted Agent tree",
        )
        await pilot.press("ctrl+o")
        await _wait_for_screen(app, ChoiceScreen, title="SESSION • USER")
        assert [choice.value for choice in app.screen._choices] == [root_session_id]
        await pilot.press("tab")
        await _wait_for_screen(app, ChoiceScreen, title="SESSION • TASK")
        assert not app.screen._choices
        await pilot.press("tab")
        await _wait_for_screen(app, ChoiceScreen, title="SESSION • SUBAGENT")
        assert [choice.value for choice in app.screen._choices] == [child_session_id]
        await pilot.press("enter")
        await _wait_until(
            lambda: app._visible_session_id() == child_session_id,
            pilot,
            description="direct child session surface from session picker",
        )

        panel = app.query_one(SubagentNavigationPanel)
        composer = app.query_one(Composer)
        assert app._controller.session_id == root_session_id
        assert app._visible_read_only is True
        assert app._visible_agent is not None
        assert app._visible_agent.session_id == child_session_id
        assert composer.disabled is True
        assert composer.has_class("read-only-session")
        assert panel.display is True
        assert panel.current_session_id == child_session_id
        assert panel.visible_session_ids == (child_session_id,)
        await pilot.press("ctrl+o")
        await _wait_for_screen(app, ChoiceScreen, title="SESSION • SUBAGENT")
        assert app.screen._choices[0].marker == DEFAULT_TUI_THEME.selected_choice_marker
        await pilot.press("escape")

        app.post_message(events.Key("alt+up", None))
        await _wait_until(
            lambda: app._visible_session_id() == root_session_id,
            pilot,
            description="root surface after direct child entry",
        )
        await _wait_for_worker_group(app, pilot, group="navigation")
        await pilot.pause()

        assert app._visible_read_only is False
        assert composer.disabled is False
        assert not composer.has_class("read-only-session")
        assert panel.current_session_id == root_session_id

        rendered_panel = panel.render_content(panel.content_region.width).plain
        child_node_x = rendered_panel.rfind("■")
        await pilot.hover(panel, offset=(child_node_x, 0))
        await pilot.pause()
        hovered_panel = panel.render_content(panel.content_region.width).plain
        assert "General" in hovered_panel
        assert "Inspect direct child navigation." in hovered_panel
        assert panel.selected_session_id == root_session_id

        await pilot.click(panel, offset=(child_node_x, 0))
        await _wait_until(
            lambda: app._visible_session_id() == child_session_id,
            pilot,
            description="child surface after clicking its navigation node",
        )
        await _wait_for_worker_group(app, pilot, group="navigation")
        await pilot.pause()

        rendered_panel = panel.render_content(panel.content_region.width).plain
        root_node_x = rendered_panel.rfind("■ | ")
        await pilot.click(panel, offset=(root_node_x, 0))
        await _wait_until(
            lambda: app._visible_session_id() == root_session_id,
            pilot,
            description="root surface after clicking its navigation node",
        )


@pytest.mark.asyncio
async def test_startup_resume_preserves_canonical_child_read_only_state(
    tmp_path: Path,
) -> None:
    application = _application(tmp_path)
    root_session_id, child_session_id = _persist_direct_child_session(application)
    app = OuroTuiApp(
        application=application,
        resume_session_id=child_session_id,
    )

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await _wait_until(
            lambda: app._visible_session_id() == child_session_id,
            pilot,
            description="startup child session surface",
        )

        composer = app.query_one(Composer)
        panel = app.query_one(SubagentNavigationPanel)
        assert app._controller.session_id == root_session_id
        assert app._visible_read_only is True
        assert composer.disabled is True
        assert composer.has_class("read-only-session")
        assert panel.display is True
        assert panel.current_session_id == child_session_id


@pytest.mark.asyncio
async def test_actions_opens_subagent_dispatch_policy(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with run_ready_app(app, size=(100, 34)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        menu = app.screen

        app.screen.query_one("#choice-filter", Input).value = "Subagent permissions"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(
            app,
            AgentDispatchPolicyScreen,
            title="SUBAGENT PERMISSIONS",
        )
        screen = app.screen
        assert isinstance(screen, AgentDispatchPolicyScreen)
        screen._replace_choices(
            screen._policy_choices(),
            selected="agent-policy-save",
        )
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        await wait_for_menu_action(pilot, menu)
        assert not list(app.query("Toast"))
        await pilot.press("escape")


@pytest.mark.asyncio
async def test_actions_lists_the_unified_subagent_catalog(tmp_path: Path) -> None:
    application = _application(tmp_path)
    assert application.agent_definitions is not None
    application.agent_definitions.create(
        agent_id="review-code",
        name="Code reviewer",
        description="Review a bounded code change.",
        system_prompt="Review the change.",
        tool_selection=ToolSelection(mode="all"),
        tools=application.runtime.tools,
        expected_revision=0,
    )
    application.agent_definitions.create(
        agent_id="write-tests",
        name="Write tests",
        description="Write regression tests for a bounded change.",
        system_prompt="Write focused regression tests.",
        tool_selection=ToolSelection(mode="all"),
        tools=application.runtime.tools,
        expected_revision=1,
    )
    app = OuroTuiApp(application=application)

    async with run_ready_app(app, size=(100, 34)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        app.screen.query_one("#choice-filter", Input).value = "Manage subagents"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="SUBAGENTS")

        assert [(choice.label, choice.group) for choice in app.screen._choices] == [
            ("General", "BUILT-IN"),
            ("Code reviewer", "USER"),
            ("Write tests", "USER"),
        ]

        general = next(choice for choice in app.screen._choices if choice.value == "general")
        assert general.group == "BUILT-IN"
        assert general.trailing == "general • available"
        assert general.disabled is True
        custom = next(choice for choice in app.screen._choices if choice.value == "review-code")
        assert custom.group == "USER"
        assert custom.disabled is False

        options = app.screen.query_one("#choice-list", OptionList)
        options.highlighted = options.get_option_index("review-code")
        options.focus()
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="SUBAGENT • Code reviewer")
        assert [choice.label for choice in app.screen._choices] == [
            "Edit subagent",
            "Delete subagent",
        ]
        assert not list(app.query("Toast"))

        await pilot.press("escape")
        await _wait_for_screen(app, ChoiceScreen, title="SUBAGENTS")
        await pilot.press("escape")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        await pilot.press("escape")


@pytest.mark.asyncio
async def test_subagent_edit_and_delete_menus_show_disabled_builtin_group(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with run_ready_app(app, size=(100, 34)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")

        for label, title in (
            ("Edit subagent", "EDIT SUBAGENT"),
            ("Delete subagent", "DELETE SUBAGENT"),
        ):
            app.screen.query_one("#choice-filter", Input).value = label
            await pilot.pause()
            await pilot.press("enter")
            await _wait_for_screen(app, ChoiceScreen, title=title)
            general = next(choice for choice in app.screen._choices if choice.value == "general")
            assert general.group == "BUILT-IN"
            assert general.disabled is True
            assert not list(app.query("Toast"))
            await pilot.press("escape")
            await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")

        await pilot.press("escape")


@pytest.mark.asyncio
async def test_subagent_crud_saves_without_success_notifications(tmp_path: Path) -> None:
    application = _application(tmp_path)
    app = OuroTuiApp(application=application)

    async with run_ready_app(app, size=(100, 34)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        menu = app.screen
        app.screen.query_one("#choice-filter", Input).value = "Create subagent"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(
            app,
            AgentDefinitionConfigurationScreen,
            title="CREATE SUBAGENT",
        )

        screen = app.screen
        assert isinstance(screen, AgentDefinitionConfigurationScreen)
        screen._values.update(
            {
                "agent-definition-id": "review-code",
                "agent-definition-name": "Code reviewer",
                "agent-definition-description": "Review a bounded code change.",
                "agent-definition-prompt": "Review the change.",
            }
        )
        screen._replace_choices(
            screen._definition_choices(),
            selected="agent-definition-save",
        )
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        await wait_for_menu_action(pilot, menu)

        assert application.agent_definitions is not None
        assert [
            item.agent_id for item in application.agent_definitions.config.snapshot().definitions
        ] == ["review-code"]
        assert not list(app.query("Toast"))

        app.screen.query_one("#choice-filter", Input).value = "Edit subagent"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="EDIT SUBAGENT")
        options = app.screen.query_one("#choice-list", OptionList)
        options.highlighted = options.get_option_index("review-code")
        options.focus()
        await pilot.press("enter")
        await _wait_for_screen(
            app,
            AgentDefinitionConfigurationScreen,
            title="EDIT SUBAGENT",
        )
        screen = app.screen
        assert isinstance(screen, AgentDefinitionConfigurationScreen)
        screen._values["agent-definition-name"] = "Senior code reviewer"
        screen._replace_choices(
            screen._definition_choices(),
            selected="agent-definition-save",
        )
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        await wait_for_menu_action(pilot, menu)
        assert (
            application.agent_definitions.config.snapshot().definitions[0].name
            == "Senior code reviewer"
        )
        assert not list(app.query("Toast"))

        app.screen.query_one("#choice-filter", Input).value = "Delete subagent"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="DELETE SUBAGENT")
        options = app.screen.query_one("#choice-list", OptionList)
        options.highlighted = options.get_option_index("review-code")
        options.focus()
        await pilot.press("enter")
        await _wait_for_screen(
            app,
            ConfirmationScreen,
            title="CONFIRM DELETE SUBAGENT",
        )
        await pilot.press("down", "enter")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        await wait_for_menu_action(pilot, menu)
        assert not application.agent_definitions.config.snapshot().definitions
        assert not list(app.query("Toast"))
        await pilot.press("escape")


@pytest.mark.asyncio
async def test_alt_menu_shortcuts_do_not_replace_transient_choice_children(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))

    async with run_ready_app(app, size=(100, 42)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        actions = app.screen
        actions.query_one("#choice-filter", Input).value = "Context compaction"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(app, CompactionConfigurationScreen, title="CONTEXT COMPACTION")
        editor = app.screen
        options = editor.query_one("#choice-list", OptionList)
        options.highlighted = options.get_option_index("compaction-model")
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="COMPACTION MODEL")
        transient = app.screen

        for key in ("alt+p", "f1", "ctrl+p", "ctrl+o", "ctrl+s", "ctrl+t"):
            await pilot.press(key)
            await pilot.pause()
            assert app.screen is transient
        assert transient.navigation_root_id is None
        assert len(app.screen_stack) == 4
        await pilot.press("escape")
        await _wait_for_screen(app, CompactionConfigurationScreen, title="CONTEXT COMPACTION")
        await pilot.press("escape")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        await pilot.press("escape")


@pytest.mark.asyncio
async def test_subagent_policy_uses_shared_grouped_inline_menu(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_application(tmp_path))
    results = []

    async with run_ready_app(app, size=(100, 34)) as pilot:
        await pilot.pause()
        current = await app._controller.agent_runtime_configuration()
        catalog = await app._controller.agent_definition_catalog()
        models = await app._controller.models()
        general_definition = catalog.definitions[0]
        screen = AgentDispatchPolicyScreen(
            current,
            definitions=(
                general_definition.model_copy(
                    update={"agent_id": "zulu", "name": "Zulu", "source": "user"}
                ),
                general_definition,
                general_definition.model_copy(
                    update={"agent_id": "alpha", "name": "Alpha", "source": "user"}
                ),
            ),
            models=models,
        )
        app.push_screen(screen, callback=results.append)
        await pilot.pause()

        assert isinstance(screen, EditableChoiceScreen)
        assert not list(screen.query("Button"))
        assert not list(screen.query("Checkbox"))
        assert not list(screen.query("Select"))
        assert {choice.group for choice in screen._choices} == {
            "ALLOWED",
            "EXECUTION",
            "CAPABILITIES",
            "ACTIONS",
        }
        assert (
            next(
                choice for choice in screen._choices if choice.value == "agent-policy-agents"
            ).edit_value
            is None
        )

        options = screen.query_one("#choice-list", OptionList)
        options.highlighted = options.get_option_index("agent-policy-agents")
        options.focus()
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="ALLOWED SUBAGENTS")
        assert [choice.value for choice in app.screen._choices] == ["general", "alpha", "zulu"]
        general = next(choice for choice in app.screen._choices if choice.value == "general")
        assert general.marker == DEFAULT_TUI_THEME.selected_choice_marker
        await pilot.press("enter")
        await pilot.pause()
        general = next(choice for choice in app.screen._choices if choice.value == "general")
        assert general.marker == ""
        await pilot.press("enter", "escape")
        await _wait_for_screen(
            app,
            AgentDispatchPolicyScreen,
            title="SUBAGENT PERMISSIONS",
        )

        options = screen.query_one("#choice-list", OptionList)
        options.highlighted = options.get_option_index("agent-policy-models")
        options.focus()
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="ALLOWED SUBAGENT MODELS")
        assert (
            next(choice for choice in app.screen._choices if choice.value == models[0].key).marker
            == DEFAULT_TUI_THEME.selected_choice_marker
        )
        await pilot.press("escape")
        await _wait_for_screen(
            app,
            AgentDispatchPolicyScreen,
            title="SUBAGENT PERMISSIONS",
        )

        await screen._select_choice("agent-policy-write")
        screen._replace_choices(
            screen._policy_choices(),
            selected="agent-policy-save",
        )
        await pilot.press("enter")
        await pilot.pause()

        assert len(results) == 1
        assert results[0].allowed_agent_id_patterns == ("alpha", "general", "zulu")
        assert results[0].allowed_model_patterns == (models[0].key,)
        assert results[0].allow_write is False


@pytest.mark.asyncio
async def test_subagent_definition_editor_does_not_limit_selected_tool_count(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))
    tools = tuple(
        AgentToolOptionView(
            name=f"tool_{index}",
            description=f"Tool {index}",
            effect="read_only",
            owner_id="ouro.host",
            execution_contract_hash=f"sha256:{index}",
        )
        for index in range(40)
    )

    async with run_ready_app(app, size=(100, 34)) as pilot:
        await pilot.pause()
        models = await app._controller.models()
        screen = AgentDefinitionConfigurationScreen(tools, models)
        app.push_screen(screen)
        await pilot.pause()

        assert isinstance(screen, EditableChoiceScreen)
        assert not list(screen.query("Button"))
        assert not list(screen.query("Checkbox"))
        assert not list(screen.query("Select"))
        save = next(choice for choice in screen._choices if choice.value == "agent-definition-save")
        assert save.trailing == "Agent ID is required"
        model_choice = next(
            choice for choice in screen._choices if choice.value == "agent-definition-model"
        )
        assert model_choice.trailing == "Use conversation model"

        options = screen.query_one("#choice-list", OptionList)
        options.highlighted = options.get_option_index("agent-definition-model")
        options.focus()
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="SUBAGENT MODEL")
        assert (
            next(
                choice for choice in app.screen._choices if choice.value == "__ouro_runtime_model__"
            ).marker
            == DEFAULT_TUI_THEME.selected_choice_marker
        )
        assert (
            next(choice for choice in app.screen._choices if choice.value == "default").disabled
            is True
        )
        screen._system_default_model = models[0]
        assert (
            next(choice for choice in screen._model_choices() if choice.value == "default").disabled
            is False
        )
        model_options = app.screen.query_one("#choice-list", OptionList)
        model_options.highlighted = model_options.get_option_index(models[0].key)
        model_options.focus()
        await pilot.press("enter")
        await _wait_for_screen(
            app,
            AgentDefinitionConfigurationScreen,
            title="CREATE SUBAGENT",
        )
        screen._values.update(
            {
                "agent-definition-id": "Invalid ID",
                "agent-definition-name": "Tool reviewer",
                "agent-definition-description": "Review tool contracts.",
                "agent-definition-prompt": "Review every selected tool contract.",
            }
        )
        error = screen._validation_error()
        assert "lowercase stable identifier" in error
        assert "pydantic.dev" not in error
        assert "validation error" not in error
        screen._values["agent-definition-id"] = "tool-reviewer"
        screen._tool_mode = "selected"
        screen._selected_tools = set(screen._tools)

        draft = screen._draft()
        tool_choices = tuple(
            choice
            for choice in screen._definition_choices()
            if choice.value.startswith("agent-definition-tool:")
        )

        assert draft.model == models[0].key
        assert draft.tool_selection.mode == "selected"
        assert len(draft.tool_selection.names) == 40
        assert all(choice.trailing == "read only" for choice in tool_choices)
        assert all("selected" not in choice.trailing for choice in tool_choices)
        await pilot.press("escape")
