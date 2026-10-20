from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from ouro.application.agent_models import AgentTreeNode
from ouro.application.supervisor_models import RuntimeActivitySnapshot
from ouro.tui.subagent_navigation import SubagentNavigationPanel


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
        lifecycle_binding="bound",
        control_state="open",
        depth=1,
        activity=RuntimeActivitySnapshot(
            session_id=f"ses_child_{index}",
            status=status,
            activity=activity_name,
        ),
    )


def test_subagent_panel_keeps_latest_ten_nodes_and_reframes_around_current_child() -> None:
    panel = SubagentNavigationPanel()
    agents = tuple(_agent(index, parent_run_id=f"run_{(index - 1) // 4}") for index in range(1, 13))

    panel.set_tree(root_session_id="ses_root", agents=agents, root_status="running")
    panel.set_current_session("ses_root")

    assert panel.visible_session_ids == tuple(f"ses_child_{index}" for index in range(3, 13))
    assert panel.render_content(80).plain.rstrip().endswith("▣ | ■■ ■■■■ ■■■■")

    panel.set_current_session("ses_child_2")

    assert panel.visible_session_ids == tuple(f"ses_child_{index}" for index in range(1, 11))
    assert "Inspect task 2 • Worker 2" in panel.render_content(80).plain
    assert panel.render_content(80).plain.rstrip().endswith("■ | ■▣■■ ■■■■ ■■")


def test_subagent_panel_keeps_its_reserved_row_when_the_tree_is_empty() -> None:
    panel = SubagentNavigationPanel()
    panel.display = True

    panel.set_tree(root_session_id="ses_root", agents=(), root_status="idle")

    assert panel.display is True
    assert panel.visible_session_ids == ()
    assert panel.render_content(20).plain == " " * 20

    panel.set_tree(
        root_session_id="ses_root",
        agents=(_agent(1, parent_run_id="run_root"),),
        root_status="running",
    )

    assert panel.display is True
    assert panel.render_content(20).plain.rstrip().endswith("▣ | ■")


def test_subagent_panel_includes_nested_agents_from_the_same_root_session() -> None:
    panel = SubagentNavigationPanel()
    direct = _agent(1, parent_run_id="run_root")
    nested = _agent(
        2,
        parent_run_id="run_child",
        parent_session_id=direct.session_id,
    )

    panel.set_tree(
        root_session_id="ses_root",
        agents=(direct, nested),
        root_status="running",
    )

    assert panel.visible_session_ids == (direct.session_id, nested.session_id)
    assert panel.render_content(80).plain.rstrip().endswith("▣ | ■ ■")


def test_subagent_panel_keeps_interleaved_dispatches_in_stable_run_groups() -> None:
    panel = SubagentNavigationPanel()
    agents = (
        _agent(1, parent_run_id="run_a"),
        _agent(2, parent_run_id="run_b"),
        _agent(3, parent_run_id="run_a"),
    )

    panel.set_tree(root_session_id="ses_root", agents=agents, root_status="running")

    assert panel.visible_session_ids == (
        agents[0].session_id,
        agents[2].session_id,
        agents[1].session_id,
    )
    assert panel.render_content(80).plain.rstrip().endswith("▣ | ■■ ■")
    panel.begin_navigation()
    assert panel.select_last_child() == agents[1].session_id


def test_subagent_panel_navigation_selects_root_last_and_adjacent_children() -> None:
    panel = SubagentNavigationPanel()
    agents = tuple(_agent(index, parent_run_id="run_root") for index in range(1, 4))
    panel.set_tree(root_session_id="ses_root", agents=agents, root_status="running")
    panel.set_current_session("ses_child_2")

    panel.begin_navigation()
    assert panel.select_relative(-1) == "ses_child_1"
    assert panel.selected_agent == agents[0]
    assert "Inspect task 1 • Worker 1" in panel.render_content(80).plain
    assert panel.select_relative(-1) == "ses_root"
    assert panel.select_relative(-1) == "ses_root"
    assert panel.select_relative(1) == "ses_child_1"
    assert panel.select_relative(1) == "ses_child_2"
    assert panel.select_root() == "ses_root"
    assert "Inspect task" not in panel.render_content(80).plain
    assert panel.select_last_child() == "ses_child_3"
    assert panel.select_relative(1) == "ses_child_3"

    panel.end_navigation()

    assert panel.navigation_active is False
    assert panel.selected_session_id == "ses_child_2"


class _SubagentNavigationApp(App[None]):
    def __init__(self, agents: tuple[AgentTreeNode, ...]) -> None:
        super().__init__()
        self.panel = SubagentNavigationPanel()
        self.panel.display = True
        self.panel.set_tree(
            root_session_id="ses_root",
            agents=agents,
            root_status="running",
        )
        self.panel.set_current_session("ses_root")
        self.navigation: list[str] = []

    def compose(self) -> ComposeResult:
        yield self.panel

    def on_subagent_navigation_panel_navigation_requested(
        self,
        message: SubagentNavigationPanel.NavigationRequested,
    ) -> None:
        self.panel.begin_navigation()
        self.panel.select_session(message.session_id)
        self.navigation.append(message.session_id)


@pytest.mark.asyncio
async def test_subagent_panel_hover_previews_stable_visible_node_and_click_requests_it() -> None:
    agents = tuple(_agent(index, parent_run_id=f"run_{(index - 1) // 4}") for index in range(1, 13))
    app = _SubagentNavigationApp(agents)

    async with app.run_test(size=(80, 6)) as pilot:
        panel = app.panel
        visible_before = panel.visible_session_ids
        rendered = panel.render_content(panel.content_region.width).plain
        nodes = rendered[rendered.rfind("▣ | ") :]
        first_child_x = rendered.rfind("▣ | ") + nodes.index("■")

        await pilot.hover(panel, offset=(first_child_x, 0))
        await pilot.pause()

        assert panel.visible_session_ids == visible_before
        assert panel.selected_session_id == "ses_root"
        assert "Inspect task 3 • Worker 3" in panel.render_content(80).plain
        assert panel.render_content(80).plain.rstrip().endswith("■ | ▣■ ■■■■ ■■■■")

        await pilot.hover(None, offset=(0, 5))
        await pilot.pause()
        assert panel.render_content(80).plain.rstrip().endswith("▣ | ■■ ■■■■ ■■■■")

        await pilot.click(panel, offset=(first_child_x, 0))
        await pilot.pause()

        assert app.navigation == ["ses_child_3"]
        assert panel.selected_session_id == "ses_child_3"

        rendered = panel.render_content(panel.content_region.width).plain
        root_node_x = rendered.rfind("■ | ")
        await pilot.click(panel, offset=(root_node_x, 0))
        await pilot.pause()

        assert app.navigation == ["ses_child_3", "ses_root"]
        assert panel.selected_session_id == "ses_root"


def test_subagent_panel_skips_equivalent_tree_and_selection_refreshes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    panel = SubagentNavigationPanel()
    agents = (_agent(1, parent_run_id="run_root"),)
    panel.set_tree(root_session_id="ses_root", agents=agents, root_status="running")
    panel.set_current_session("ses_child_1")

    refreshes = 0

    def record_refresh() -> None:
        nonlocal refreshes
        refreshes += 1

    monkeypatch.setattr(panel, "_refresh_content", record_refresh)

    panel.set_tree(root_session_id="ses_root", agents=agents, root_status="running")
    panel.set_current_session("ses_child_1")

    assert refreshes == 0
