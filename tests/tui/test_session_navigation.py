"""Interaction and layout of the session navigation row."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from ouro.application.contracts.agents import AgentTreeNode
from ouro.application.contracts.execution import RuntimeActivitySnapshot
from ouro.application.contracts.sessions import SessionCatalogEntry
from ouro.core.base import utc_now
from ouro.tui.session_navigation import SessionNavigationPanel
from ouro.tui.theme import theme_for


def _agent(
    index: int,
    *,
    parent_run_id: str,
    parent_session_id: str = "ses_root",
    status: str = "running",
) -> AgentTreeNode:
    activity_name = {
        "completed": "completed",
        "failed": "failed",
    }.get(status, "streaming_text")
    return AgentTreeNode(
        revision=1,
        delegation_id=f"dlg_{index}",
        root_session_id="ses_root",
        session_id=f"ses_child_{index}",
        parent_session_id=parent_session_id,
        parent_run_id=parent_run_id,
        parent_tool_call_id=f"spawn_{index}",
        agent_id="general",
        agent_name=f"General {index}",
        nickname=f"Worker {index}",
        task_description=f"Inspect task {index}",
        status=status,
        execution_mode="async" if index % 2 else "sync",
        control_state="open",
        activity=RuntimeActivitySnapshot(
            session_id=f"ses_child_{index}",
            status=status,
            activity=activity_name,
        ),
    )


def test_session_panel_keeps_latest_ten_nodes_and_reframes_around_current_child() -> None:
    panel = SessionNavigationPanel()
    agents = tuple(_agent(index, parent_run_id=f"run_{(index - 1) // 4}") for index in range(1, 13))

    panel.set_nodes(main_session_id="ses_root", subagents=agents, main_status="running")
    panel.set_current_session("ses_root")

    assert panel.visible_related_session_ids == tuple(
        f"ses_child_{index}" for index in range(3, 13)
    )

    panel.set_current_session("ses_child_2")

    assert panel.visible_related_session_ids == tuple(
        f"ses_child_{index}" for index in range(1, 11)
    )
    assert "Inspect task 2 • Worker 2" in panel.render_content(80).plain


def test_session_panel_includes_nested_agents_from_the_same_root_session() -> None:
    panel = SessionNavigationPanel()
    direct = _agent(1, parent_run_id="run_root")
    nested = _agent(
        2,
        parent_run_id="run_child",
        parent_session_id=direct.session_id,
    )

    panel.set_nodes(
        main_session_id="ses_root",
        subagents=(direct, nested),
        main_status="running",
    )

    assert panel.visible_related_session_ids == (direct.session_id, nested.session_id)


def test_session_panel_keeps_interleaved_dispatches_in_stable_run_groups() -> None:
    panel = SessionNavigationPanel()
    agents = (
        _agent(1, parent_run_id="run_a"),
        _agent(2, parent_run_id="run_b"),
        _agent(3, parent_run_id="run_a"),
    )

    panel.set_nodes(main_session_id="ses_root", subagents=agents, main_status="running")

    assert panel.visible_related_session_ids == (
        agents[0].session_id,
        agents[2].session_id,
        agents[1].session_id,
    )
    panel.begin_navigation()
    assert panel.select_last_node() == agents[1].session_id


def test_session_panel_navigation_selects_root_last_and_adjacent_children() -> None:
    panel = SessionNavigationPanel()
    agents = tuple(_agent(index, parent_run_id="run_root") for index in range(1, 4))
    panel.set_nodes(main_session_id="ses_root", subagents=agents, main_status="running")
    panel.set_current_session("ses_child_2")

    panel.begin_navigation()
    assert panel.select_relative(-1) == "ses_child_1"
    assert "Inspect task 1 • Worker 1" in panel.render_content(80).plain
    assert panel.select_relative(-1) == "ses_root"
    assert panel.select_relative(-1) == "ses_root"
    assert panel.select_relative(1) == "ses_child_1"
    assert panel.select_relative(1) == "ses_child_2"
    assert panel.select_main() == "ses_root"
    assert "Inspect task" not in panel.render_content(80).plain
    assert panel.select_last_node() == "ses_child_3"
    assert panel.select_relative(1) == "ses_child_3"

    panel.end_navigation()

    assert panel.navigation_active is False
    assert panel.selected_session_id == "ses_child_2"


class _SessionNavigationApp(App[None]):
    def __init__(self, agents: tuple[AgentTreeNode, ...]) -> None:
        super().__init__()
        self.panel = SessionNavigationPanel()
        self.panel.display = True
        self.panel.set_nodes(
            main_session_id="ses_root",
            subagents=agents,
            main_status="running",
        )
        self.panel.set_current_session("ses_root")
        self.navigation: list[str] = []

    def compose(self) -> ComposeResult:
        yield self.panel

    def on_session_navigation_panel_navigation_requested(
        self,
        message: SessionNavigationPanel.NavigationRequested,
    ) -> None:
        self.panel.begin_navigation()
        self.panel.select_session(message.session_id)
        self.navigation.append(message.session_id)


@pytest.mark.asyncio
async def test_side_session_click_and_keyboard_navigation_reach_neighboring_sessions() -> None:
    agents = tuple(_agent(i, parent_run_id="run_root") for i in range(3))
    sides = tuple(
        SessionCatalogEntry(
            session_id=f"ses_side_{i}",
            root_session_id=f"ses_side_{i}",
            context_source_session_id="ses_root",
            title=f"Side {i}",
            created_at=utc_now(),
            updated_at=utc_now(),
            last_activity_at=utc_now(),
            status="idle",
        )
        for i in range(2)
    )
    app = _SessionNavigationApp(agents)
    async with app.run_test(size=(80, 6)) as pilot:
        panel = app.panel
        panel.set_nodes(
            main_session_id="ses_root", subagents=agents, main_status="running", side_sessions=sides
        )
        row = panel.render_content(80).plain
        x = row.index(theme_for(panel).session_node_mark)
        await pilot.hover(panel, offset=(x, 0))
        assert panel.current_session_id == panel.selected_session_id == "ses_root"
        assert "Side 0" in panel.render_content(80).plain
        await pilot.click(panel, offset=(x, 0))
        assert app.navigation == ["ses_side_0"]
        assert panel.selected_session_id == sides[0].session_id
        panel.set_current_session("ses_side_0")
        assert panel.select_relative(1) == "ses_side_1"
        assert panel.select_relative(1) == "ses_root"
        assert panel.select_relative(1) == agents[0].session_id


@pytest.mark.asyncio
async def test_session_panel_hover_previews_stable_visible_node_and_click_requests_it() -> None:
    agents = tuple(_agent(index, parent_run_id=f"run_{(index - 1) // 4}") for index in range(1, 13))
    app = _SessionNavigationApp(agents)

    async with app.run_test(size=(80, 6)) as pilot:
        panel = app.panel
        visible_before = panel.visible_related_session_ids
        rendered = panel.render_content(panel.content_region.width).plain
        node_mark = theme_for(panel).session_node_mark
        first_child_x = rendered.index(node_mark)

        await pilot.hover(panel, offset=(first_child_x, 0))
        await pilot.pause()

        assert panel.visible_related_session_ids == visible_before
        assert panel.selected_session_id == "ses_root"
        assert "Inspect task 3 • Worker 3" in panel.render_content(80).plain

        await pilot.hover(None, offset=(0, 5))
        await pilot.pause()
        assert "Inspect task 3" not in panel.render_content(80).plain

        await pilot.click(panel, offset=(first_child_x, 0))
        await pilot.pause()

        assert app.navigation == ["ses_child_3"]
        assert panel.selected_session_id == "ses_child_3"
        panel.set_current_session("ses_child_3")

        rendered = panel.render_content(panel.content_region.width).plain
        root_node_x = rendered.index(node_mark)
        await pilot.click(panel, offset=(root_node_x, 0))
        await pilot.pause()

        assert app.navigation == ["ses_child_3", "ses_root"]
        assert panel.selected_session_id == "ses_root"


def test_session_panel_skips_equivalent_tree_and_selection_refreshes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    panel = SessionNavigationPanel()
    agents = (_agent(1, parent_run_id="run_root"),)
    panel.set_nodes(main_session_id="ses_root", subagents=agents, main_status="running")
    panel.set_current_session("ses_child_1")

    refreshes = 0

    def record_refresh() -> None:
        nonlocal refreshes
        refreshes += 1

    monkeypatch.setattr(panel, "_refresh_content", record_refresh)

    panel.set_nodes(main_session_id="ses_root", subagents=agents, main_status="running")
    panel.set_current_session("ses_child_1")

    assert refreshes == 0
